#! /usr/bin/python2.7

import json
import h5py

from NetworkDescription import LayerNetworkDescription
from NetworkBaseLayer import Layer, SourceLayer
from NetworkLayer import get_layer_class
from NetworkLstmLayer import *
from NetworkOutputLayer import OutputLayer, FramewiseOutputLayer, SequenceOutputLayer, DecoderOutputLayer, UnsupervisedOutputLayer
from Util import collect_class_init_kwargs
from Log import log

class LayerNetwork(object):
  def __init__(self, n_in=None, n_out=None, base_network=None, data_map=None, data_map_i=None):
    """
    :param int n_in: input dim of the network
    :param dict[str,(int,int)] n_out: output dim of the network.
      first int is num classes, second int is 1 if it is sparse, i.e. we will get the indices.
    :param dict[str,theano.Variable] data_map: if specified, this will be used for x/y (and it expects data_map_i)
    :param dict[str,theano.Variable] data_map_i: if specified, this will be used for i/j
    :param LayerNetwork base_network: optional base network where we will derive x/y/i/j/n_in/n_out from.
      data_map will have precedence over base_network.
    """
    if n_out is None:
      assert base_network is not None
      n_out = base_network.n_out
    else:
      assert n_out is not None
      n_out = n_out.copy()
    if n_in is None:
      assert "data" in n_out
      n_in = n_out["data"][0]
    if "data" not in n_out:
      data_dim = 3
      n_out["data"] = (n_in, data_dim - 1)  # small hack: support input-data as target
    else:
      assert 1 <= n_out["data"][1] <= 2  # maybe obsolete check...
      data_dim = n_out["data"][1] + 1  # one more because of batch-dim
    if data_map is not None:
      assert data_map_i is not None
      self.y = data_map
      self.x = data_map["data"]
      self.j = data_map_i
      self.i = data_map_i["data"]
    elif base_network is not None:
      self.x = base_network.x
      self.y = base_network.y
      self.i = base_network.i
      self.j = base_network.j
    else:
      dtype = "float32" if data_dim >= 3 else "int32"
      self.x = T.TensorType(dtype, ((False,) * data_dim))('x')
      self.y = {"data": self.x}
      self.i = T.bmatrix('i'); """ :type: theano.Variable """
      self.j = {"data": self.i}
    if base_network is not None:
      self.epoch = base_network.epoch
    else:
      self.epoch = T.constant(0, name="epoch", dtype="int32")
    self.constraints = T.constant(0)
    Layer.initialize_rng()
    self.n_in = n_in
    self.n_out = n_out
    self.hidden = {}; """ :type: dict[str,ForwardLayer|RecurrentLayer] """
    self.train_params_vars = []; """ :type: list[theano.compile.sharedvalue.SharedVariable] """
    self.description = None; """ :type: LayerNetworkDescription | None """
    self.train_param_args = None; """ :type: dict[str] """
    self.recurrent = False  # any of the from_...() functions will set this
    self.output = {}; " :type: dict[str,FramewiseOutputLayer] "
    self.known_grads = {}; " :type: dict[theano.Variable,theano.Variable]"
    self.json_content = "{}"
    self.costs = {}
    self.total_cost = T.constant(0)
    self.update_step = 0
    self.errors = {}
    self.loss = None
    self.ctc_priors = None
    self.default_mask = None  # any of the from_...() functions will set this
    self.sparse_input = None  # any of the from_...() functions will set this
    self.default_target = None  # any of the from_...() functions will set this
    self.train_flag = None  # any of the from_...() functions will set this
    self.get_layer_param = None  # used by Container.add_param()
    self.calc_step_base = None
    self.calc_steps = []
    self.base_network = base_network

  @classmethod
  def from_config_topology(cls, config, mask=None, train_flag=False, eval_flag=False):
    """
    :type config: Config.Config
    :param str mask: e.g. "unity" or None ("dropout"). "unity" is for testing.
    :rtype: LayerNetwork
    """
    json_content = cls.json_from_config(config, mask=mask)
    return cls.from_json_and_config(json_content, config, mask=mask, train_flag=train_flag, eval_flag=eval_flag)

  @classmethod
  def json_from_config(cls, config, mask=None):
    """
    :type config: Config.Config
    :param str mask: "unity", "none" or "dropout"
    :rtype: dict[str]
    """
    json_content = None
    if config.has("network") and config.is_typed("network"):
      json_content = config.typed_value("network")
      assert isinstance(json_content, dict)
      assert json_content
    elif config.network_topology_json:
      start_var = config.network_topology_json.find('(config:', 0) # e.g. ..., "n_out" : (config:var), ...
      while start_var > 0:
        end_var = config.network_topology_json.find(')', start_var)
        assert end_var > 0, "invalid variable syntax at " + str(start_var)
        var = config.network_topology_json[start_var+8:end_var]
        assert config.has(var), "could not find variable " + var
        config.network_topology_json = config.network_topology_json[:start_var] + config.value(var,"") + config.network_topology_json[end_var+1:]
        print >> log.v4, "substituting variable %s with %s" % (var,config.value(var,""))
        start_var = config.network_topology_json.find('(config:', start_var+1)
      try:
        json_content = json.loads(config.network_topology_json)
      except ValueError as e:
        print >> log.v3, "----- BEGIN JSON CONTENT -----"
        print >> log.v3, config.network_topology_json
        print >> log.v3, "------ END JSON CONTENT ------"
        assert False, "invalid json content, %r" % e
      assert isinstance(json_content, dict)
      if 'network' in json_content:
        json_content = json_content['network']
      assert json_content
    if not json_content:
      if not mask:
        if sum(config.float_list('dropout', [0])) > 0.0:
          mask = "dropout"
      description = LayerNetworkDescription.from_config(config)
      json_content = description.to_json_content(mask=mask)
    return json_content

  @classmethod
  def from_description(cls, description, mask=None, train_flag = False):
    """
    :type description: NetworkDescription.LayerNetworkDescription
    :param str mask: e.g. "unity" or None ("dropout")
    :rtype: LayerNetwork
    """
    json_content = description.to_json_content(mask=mask)
    network = cls.from_json(json_content, mask=mask, train_flag=train_flag,
                            n_in=description.num_inputs, n_out=description.num_outputs)
    return network

  @classmethod
  def init_args_from_config(cls, config):
    """
    :rtype: dict[str]
    :returns the kwarg for cls.from_json()
    """
    num_inputs, num_outputs = LayerNetworkDescription.num_inputs_outputs_from_config(config)
    return {
      "n_in": num_inputs, "n_out": num_outputs,
      "sparse_input": config.bool("sparse_input", False),
      "target": config.value('target', 'classes')
    }

  @classmethod
  def from_json_and_config(cls, json_content, config, mask=None, train_flag=False, eval_flag=False):
    """
    :type config: Config.Config
    :type json_content: str | dict
    :param str mask: e.g. "unity" or None ("dropout"). "unity" is for testing.
    :rtype: LayerNetwork
    """
    return cls.from_json(json_content, mask=mask, train_flag=train_flag, eval_flag=eval_flag,
                         **cls.init_args_from_config(config))

  @classmethod
  def from_base_network(cls, base_network, json_content=None, share_params=False, base_as_calc_step=False, **kwargs):
    """
    :param LayerNetwork base_network: base network to derive from
    :param dict[str]|None json_content: JSON content for subnetwork. if None, will use from base network
    :param bool share_params: will use the same params as the base network
    :param bool base_as_calc_step: base is calc step 0. see below
    :param dict[str] kwargs: kwargs for __init__
    :rtype: LayerNetwork
    """
    network = cls(base_network=base_network, **kwargs)
    network.default_mask = base_network.default_mask
    network.sparse_input = base_network.sparse_input
    network.default_target = base_network.default_target
    network.train_flag = base_network.train_flag
    if base_as_calc_step:
      network.calc_step_base = base_network  # used by CalcStepLayer. see also get_calc_step()
    if share_params:
      def shared_get_layer_param(layer_name, param_name, param):
        if base_network.get_layer_param:
          substitute = base_network.get_layer_param(layer_name=layer_name, param_name=param_name, param=param)
          if substitute: return substitute
        base_layer = base_network.get_layer(layer_name)
        assert base_layer, "%s not found in base_network" % layer_name
        return base_layer.params.get(param_name, None)
      network.get_layer_param = shared_get_layer_param
    if json_content is None:
      json_content = base_network.to_json_content()
    cls.from_json(json_content, network=network, train_flag=base_network.train_flag, eval_flag=base_network.eval_flag)
    if share_params:
      trainable_params = network.get_all_params_vars()
      assert len(trainable_params) == 0
    return network

  def get_calc_step(self, i):
    """
    :param int i: calc step, 0 to n
    :rtype: LayerNetwork
    Used by CalcStepLayer. Will automatically create the requested calc step.
    Calc step 0 is the base network (calc_step_base).
    """
    if self.calc_step_base:
      return self.calc_step_base.get_calc_step(i)  # go up to the main network
    if i == 0: return self
    if i <= len(self.calc_steps):
      return self.calc_steps[i - 1]
    print >> log.v4, "creating calc steps up to %i" % i
    while i > len(self.calc_steps):
      base_network = self
      if self.calc_steps: base_network = self.calc_steps[-1]
      subnetwork = self.from_base_network(
        base_network=base_network, share_params=True, base_as_calc_step=True)
      self.calc_steps += [subnetwork]
    return self.calc_steps[i - 1]

  def new_subnetwork(self, json_content, n_out, data_map, data_map_i):
    """
    :param dict[str,dict] json_content: subnetwork specification
    :param dict[str,list[int,int]] n_out: n_out info for subnetwork
    :param dict[str,theano.Variable] data_map: data
    :param dict[str,theano.Variable] data_map_i: indices for data
    :rtype: LayerNetwork
    The data input for the subnetwork is not derived from ourselves but specified
    explicitly through n_out & data_map.
    """
    return self.from_base_network(self, json_content=json_content,
                                  n_out=n_out, data_map=data_map, data_map_i=data_map_i)

  @classmethod
  def from_json(cls, json_content, n_in=None, n_out=None, network=None,
                mask=None, sparse_input=False, target='classes', train_flag=False, eval_flag=False):
    """
    :type json_content: dict[str]
    :type n_in: int | None
    :type n_out: dict[str,(int,int)] | None
    :param LayerNetwork | None network: optional already existing instance
    :param str mask: e.g. "unity" or None ("dropout")
    :rtype: LayerNetwork
    """
    if network is None:
      network = cls(n_in=n_in, n_out=n_out)
      network.json_content = json.dumps(json_content, sort_keys=True)
      network.recurrent = False
      network.default_mask = mask
      network.sparse_input = sparse_input
      network.default_target = target
      network.train_flag = train_flag
      network.eval_flag = eval_flag
    n_in = network.n_in
    n_out = network.n_out
    assert isinstance(json_content, dict)
    network.y['data'].n_out = network.n_out['data'][0]
    if hasattr(LstmLayer, 'sharpgates'):
      del LstmLayer.sharpgates
    def traverse(content, layer_name, target, output_index):
      if layer_name in network.hidden:
        return network.hidden[layer_name].index
      if layer_name in network.output:
        return network.output[layer_name].index
      source = []
      obj = content[layer_name].copy()
      cl = obj.pop('class', None)
      index = output_index
      if 'target' in obj:
        target = obj['target']
      dtype = obj.get("dtype", "int32")
      network.use_target(target, dtype=dtype)
      if not 'from' in obj:
        source = [SourceLayer(network.n_in, network.x, sparse=sparse_input, name='data', index=network.i)]
        index = network.i
      elif obj['from']:
        if not isinstance(obj['from'], list):
          obj['from'] = [ obj['from'] ]
        for prev in obj['from']:
          if prev == 'data':
            source.append(SourceLayer(network.n_in, network.x, sparse=sparse_input, name='data', index=network.i))
            index = network.i
          elif prev != "null":
            index = traverse(content, prev, target, index)
            source.append(network.get_layer(prev))
      if 'encoder' in obj:
        encoder = []
        if not isinstance(obj['encoder'], list):
          obj['encoder'] = [obj['encoder']]
        for prev in obj['encoder']:
          traverse(content, prev, target, index)
          encoder.append(network.get_layer(prev))
        obj['encoder'] = encoder
      if 'base' in obj: # TODO(doetsch) string/layer transform should be smarter
        base = []
        if not isinstance(obj['base'], list):
          if ',' in obj['base']:
            obj['base'] = obj['base'].split(',')
          else:
            obj['base'] = [obj['base']]
        for prev in obj['base']:
          if prev == 'data':
            base.append(SourceLayer(network.n_in, network.x, sparse=sparse_input, name='data', index=network.i))
          else:
            traverse(content, prev, target, index)
            base.append(network.get_layer(prev))
        obj['base'] = base
      for key in [ 'copy_input', 'copy_output' ]:
        if key in obj:
          index = traverse(content, obj[key], target, index)
          obj[key] = network.get_layer(obj[key])
      if 'encoder' in obj and not source:
        index = output_index
      if 'target' in obj:
        index = network.j[obj['target']]
      obj.pop('from', None)
      params = { 'sources': source,
                 'dropout' : 0.0,
                 'name' : layer_name,
                 "train_flag": train_flag,
                 "eval_flag": eval_flag,
                 'network': network }
      params.update(obj)
      params["mask"] = mask # overwrite
      params['index'] = index
      params['y_in'] = network.y
      if cl == 'softmax' or cl == 'decoder':
        if not 'target' in params:
          params['target'] = target
        if 'loss' in obj and obj['loss'] == 'ctc':
          params['index'] = network.i
        else:
          params['index'] = network.j[target] #output_index
        return network.make_classifier(**params)
      else:
        layer_class = get_layer_class(cl)
        params.update({'name': layer_name})
        if layer_class.recurrent:
          network.recurrent = True
        return network.add_layer(layer_class(**params)).index
    for layer_name in json_content:
      if layer_name in network.hidden or layer_name in network.output:
        continue
      if layer_name == "data":
        print >>log.v3, "warning: layer with name 'data' will be ignored (this name is reserved)"
        continue
      trg = target
      if 'target' in json_content[layer_name]:
        trg = json_content[layer_name]['target']
      if layer_name == 'output' or 'target' in json_content[layer_name]:
        network.use_target(trg, dtype=json_content.get("dtype", "int32"))
        traverse(json_content, layer_name, trg, network.j[trg])
    return network

  @classmethod
  def from_hdf_model_topology(cls, model, n_in=None, n_out=None, input_mask=None, sparse_input=False, target='classes',
                              train_flag=False, eval_flag=False):
    """
    :type model: h5py.File
    :param str mask: e.g. "unity"
    :rtype: LayerNetwork
    """
    grp = model['training']
    n_out_model = {}
    try:
      for k in model['n_out'].attrs:
        dim = 1 if not 'dim' in model['n_out'] else model['n_out/dim'].attrs[k]
        n_out_model[k] = [model['n_out'].attrs[k], dim]
    except Exception:
      n_out_model = {'classes':[model.attrs['n_out'],1]}
    n_in_model = model.attrs['n_in']
    n_out_model.pop('data')
    if n_in and n_in != n_in_model:
      print >> log.v4, "Different HDF n_in:", n_in, n_in_model  # or error?
    if n_out and n_out != n_out_model:
      print >> log.v4, "Different HDF n_out:", n_out, n_out_model  # or error?
    network = cls(n_in_model, n_out_model)
    network.recurrent = False
    network.default_mask = input_mask
    network.sparse_input = sparse_input
    network.default_target = target
    network.train_flag = train_flag
    network.eval_flag = eval_flag

    if 'target' in model['n_out'].attrs:
      target = model['n_out'].attrs['target']
    dtype = 'int32' if not 'dtype' in model['n_out'].attrs else model['n_out'].attrs['dtype']
    if target != "null" and target not in network.y:
      network.use_target(target, dtype=dtype)
    network.y['data'].n_out = network.n_out['data'][0]
    def traverse(model, layer_name, output_index):
      index = output_index
      mask = input_mask
      if not input_mask and 'mask' in model[layer_name].attrs:
        mask = model[layer_name].attrs['mask']
      if 'from' in model[layer_name].attrs:
        x_in = []
        for s in model[layer_name].attrs['from'].split(','):
          if s == 'data':
            x_in.append(SourceLayer(network.n_in, network.x, sparse=sparse_input, name='data', index=network.i))
            index = network.i
          elif s != "null" and s != "": # this is allowed, recurrent states can be passed as input
            if not network.hidden.has_key(s):
              index = traverse(model, s, index)
            else:
              index = network.hidden[s].index
            x_in.append(network.hidden[s])
          elif s == "":
            assert not s
            # Fix for old models via NetworkDescription.
            s = Layer.guess_source_layer_name(layer_name)
            if not s:
              # Fix for data input. Just like in NetworkDescription, so that param names are correct.
              x_in.append(SourceLayer(n_out=network.n_in, x_out=network.x, name="", index=network.i))
            else:
              if not network.hidden.has_key(s):
                index = traverse(model, s, index)
              else:
                index = network.hidden[s].index
              # Add just like in NetworkDescription, so that param names are correct.
              x_in.append(SourceLayer(n_out=network.hidden[s].attrs['n_out'], x_out=network.hidden[s].output, name="", index=network.i))
      else:
        x_in = [ SourceLayer(network.n_in, network.x, sparse=sparse_input, name='data', index=network.i) ]
      if 'encoder' in model[layer_name].attrs:
        encoder = []
        for s in model[layer_name].attrs['encoder'].split(','):
          if s != "":
            if not network.hidden.has_key(s):
              traverse(model, s, index)
            encoder.append(network.hidden[s])
      if 'base' in model[layer_name].attrs: # TODO see json
        base = []
        for s in model[layer_name].attrs['base'].split(','):
          if s != "":
            if not network.hidden.has_key(s):
              traverse(model, s, index)
            base.append(network.hidden[s])
      for key in ['copy_input', 'copy_output']:
        if key in model[layer_name].attrs:
          index = traverse(model, model[layer_name].attrs[key], index)
          if key == 'copy_input':
            copy_input = network.hidden[model[layer_name].attrs[key]]
          if key == 'copy_output':
            copy_output = network.hidden[model[layer_name].attrs[key]]
      if 'encoder' in model[layer_name].attrs and not x_in:
        index = output_index
      if 'target' in model[layer_name].attrs:
        target = model[layer_name].attrs['target']
        if target != "null" and target not in network.y:
          network.use_target(target, dtype=dtype)
          index = network.j[target]
      cl = model[layer_name].attrs['class']
      if cl == 'softmax':
        params = { 'dropout' : 0.0,
                   'name' : 'output',
                   'mask' : mask,
                   'train_flag' : train_flag }
        params.update(model[layer_name].attrs)
        if 'encoder' in model[layer_name].attrs:
          params['encoder'] = encoder #network.hidden[model[layer_name].attrs['encoder']] if model[layer_name].attrs['encoder'] in network.hidden else network.output[model[layer_name].attrs['encoder']]
        if 'base' in model[layer_name].attrs:
          params['base'] = base
        if 'copy_input' in model[layer_name].attrs:
          params['copy_input'] = copy_input
        if 'copy_output' in model[layer_name].attrs:
          params['copy_output'] = copy_output
        #if not 'target' in params:
        #  params['target'] = target
        params['index'] = index #output_index
        params['sources'] = x_in
        params['y_in'] = network.y
        params.pop('from', None)
        params.pop('class', None)
        network.make_classifier(**params)
      else:
        params = { 'sources': x_in,
                   'n_out': model[layer_name].attrs['n_out'],
                   'dropout': model[layer_name].attrs['dropout'] if train_flag else 0.0,
                   'name': layer_name,
                   'mask': mask,
                   'train_flag' : train_flag,
                   "eval_flag": eval_flag,
                   'network': network,
                   'index' : index }
        try:
          act = model[layer_name].attrs['activation']
          params["activation"] = act
        except Exception:
          pass
        params['y_in'] = network.y
        layer_class = get_layer_class(cl)
        for p in collect_class_init_kwargs(layer_class):
          if p in params: continue  # don't overwrite existing
          if p in model[layer_name].attrs.keys():
            params[p] = model[layer_name].attrs[p]
        if 'encoder' in model[layer_name].attrs:
          params['encoder'] = encoder #network.hidden[model[layer_name].attrs['encoder']] if model[layer_name].attrs['encoder'] in network.hidden else network.output[model[layer_name].attrs['encoder']]
        if 'base' in model[layer_name].attrs:
          params['base'] = base

        if 'target' in model[layer_name].attrs:
          params['target'] = model[layer_name].attrs['target']
        if layer_class.recurrent:
          network.recurrent = True
        return network.add_layer(layer_class(**params)).index

    for layer_name in model:
      target = 'classes'
      if 'target' in model[layer_name].attrs:
        target = model[layer_name].attrs['target']
      if target != "null" and target not in network.y:
        network.use_target(target, dtype=dtype)
      if layer_name == 'output' or 'target' in model[layer_name].attrs:
        traverse(model, layer_name, network.j[target])
    return network

  def use_target(self, target, dtype):
    if target in self.y: return
    if target == "null": return
    if target == 'sizes' and not 'sizes' in self.n_out: #TODO(voigtlaender): fix data please
      self.n_out['sizes'] = [2,1]
    if self.base_network:
      self.base_network.use_target(target=target, dtype=dtype)
      if not self.y is self.base_network.y:
        self.y[target] = self.base_network.y[target]
      if not self.j is self.base_network.j:
        self.j[target] = self.base_network.j[target]
      if target not in self.n_out:
        self.n_out[target] = self.base_network.n_out[target]
      return
    if target.endswith("[sparse:coo]"):
      tprefix = target[:target.index("[")]
      ndim = self.n_out[target][1]  # expected (without batch), e.g. 2 if like (time,feature)
      # For each coordinate axe. Also with batch-dim.
      for i in range(ndim):
        self.y["%s[sparse:coo:%i:%i]" % (tprefix, ndim, i)] = T.TensorType("int32", (False,) * 2)('y_%s[sparse:coo:%i:%i]' % (tprefix, ndim, i))
      # And the data itself. Also with batch-dim.
      self.y["%s[sparse:coo:%i:%i]" % (tprefix, ndim, ndim)] = \
        T.TensorType(dtype, (False,) * 2)("y_%s[%i]" % (tprefix, ndim))
      # self.j will be used to get the list of keys we need to get from the dataset.
      for i in range(ndim + 1):
        self.j.setdefault("%s[sparse:coo:%i:%i]" % (tprefix, ndim, i), T.bmatrix('j_%s[sparse:coo:%i:%i]' % (tprefix, ndim, i)))
      # self.y[target] will be given to the OutputLayer.
      self.y[target] = tuple(self.y["%s[sparse:coo:%i:%i]" % (tprefix, ndim, i)] for i in range(ndim + 1))
      self.j[target] = self.j["data"]  # Not sure if this is the best we can do...
      return
    assert target in self.n_out
    ndim = self.n_out[target][1] + 1  # one more because of batch-dim
    self.y[target] = T.TensorType(dtype, (False,) * ndim)('y_%s' % target)
    self.y[target].n_out = self.n_out[target][0]
    self.j.setdefault(target, T.bmatrix('j_%s' % target))
    if getattr(self.y[target].tag, "test_value", None) is None:
      if ndim == 2:
        self.y[target].tag.test_value = numpy.zeros((3,2), dtype='int32')
      elif ndim == 3:
        self.y[target].tag.test_value = numpy.random.rand(3,2,self.n_out[target][0]).astype('float32')
    if getattr(self.j[target].tag, "test_value", None) is None:
      self.j[target].tag.test_value = numpy.ones((3,2), dtype="int8")

  def get_layer(self, layer_name):
    if layer_name in self.hidden:
      return self.hidden[layer_name]
    if layer_name in self.output:
      return self.output[layer_name]
    return None

  def add_layer(self, layer):
    """
    :type layer: NetworkHiddenLayer.Layer
    :rtype NetworkHiddenLayer.Layer
    """
    assert layer.name
    layer_errors = layer.errors()
    if layer.name == "output" or layer_errors is not None:
      is_output_layer = True
      self.output[layer.name] = layer
    else:
      is_output_layer = False
      self.hidden[layer.name] = layer
    self.add_cost_and_constraints(layer)
    if layer_errors is not None:
      self.errors[layer.name] = layer_errors
    if is_output_layer:
      if getattr(layer, "p_y_given_x", None) is None:
        # Small little hack for layers which we use as output-layers which don't set this.
        from TheanoUtil import time_batch_make_flat
        layer.p_y_given_x = time_batch_make_flat(layer.output)
      self.declare_train_params()
    return layer

  def add_cost_and_constraints(self, layer):
    self.constraints += layer.make_constraints()
    cost = layer.cost()
    if cost[0]:
      self.costs[layer.name] = cost[0]
      self.total_cost += self.costs[layer.name] * layer.cost_scale()
    if cost[1]:
      self.known_grads.update(cost[1])
    if len(cost) > 2:
      if self.ctc_priors:
        raise Exception("multiple ctc_priors, second one from layer %s" % layer.name)
      self.ctc_priors = cost[2]
      assert self.ctc_priors is not None

  def make_classifier(self, name='output', target='classes', **kwargs):
    """
    :param list[NetworkBaseLayer.Layer] sources: source layers
    :param str loss: loss type, "ce", "ctc" etc
    """
    if not "loss" in kwargs: kwargs["loss"] = "ce"
    self.loss = kwargs["loss"]
    if self.loss in ('ctc', 'ce_ctc', 'ctc2', 'sprint'):
      layer_class = SequenceOutputLayer
    elif self.loss == 'decode':
      layer_class = DecoderOutputLayer
    elif self.loss == 'unsupervised':
      layer_class = UnsupervisedOutputLayer
    else:
      layer_class = FramewiseOutputLayer

    dtype = kwargs.pop('dtype', 'int32')
    if target != "null" and target not in self.y:
      self.use_target(target, dtype=dtype)
    if target != "null":
      targets = self.y[target]
    else:
      targets = None
    if self.loss == "ctc":
      self.n_out[target][0] += 1
    if 'n_symbols' in kwargs:
      kwargs.setdefault('n_out', kwargs.pop('n_symbols'))
    elif target != "null":
      kwargs.setdefault('n_out', self.n_out[target][0])
    layer = layer_class(name=name, target=target, y=targets, dtype=dtype, **kwargs)
    self.add_layer(layer)
    return layer.index

  def get_objective(self):
    return self.total_cost + self.constraints

  def get_params_vars(self, hidden_layer_selection, with_output):
    """
    :type hidden_layer_selection: list[str]
    :type with_output: bool
    :rtype: list[theano.compile.sharedvalue.SharedVariable]
    :returns list (with well-defined order) of shared variables
    """
    params = []
    """ :type: list[theano.compile.sharedvalue.SharedVariable] """
    for name in sorted(hidden_layer_selection):
      params += self.hidden[name].get_params_vars()
    if with_output:
      for name in self.output:
        params += self.output[name].get_params_vars()
    return params

  def get_all_params_vars(self):
    return self.get_params_vars(hidden_layer_selection=sorted(self.hidden.keys()), with_output=True)

  def get_train_param_args_default(self):
    """
    :returns default kwargs for self.get_params(), which returns all params with this.
    """
    return {
      "hidden_layer_selection": [name for (name, layer) in sorted(self.hidden.items())
                                 if layer.attrs.get("trainable", True)],  # Use all.
      "with_output": True
    }

  def declare_train_params(self, **kwargs):
    """
    Kwargs as in self.get_params(), or default values.
    """
    # Set default values, also for None.
    for key, value in self.get_train_param_args_default().items():
      if kwargs.get(key, None) is None:
        kwargs[key] = value
    # Force a unique representation of kwargs.
    kwargs["hidden_layer_selection"] = sorted(kwargs["hidden_layer_selection"])
    self.train_param_args = kwargs
    self.train_params_vars = self.get_params_vars(**kwargs)

  def num_params(self):
    return sum([self.hidden[h].num_params() for h in self.hidden]) + sum([self.output[k].num_params() for k in self.output])

  def get_params_dict(self):
    """
    :rtype: dict[str,dict[str,numpy.ndarray|theano.sandbox.cuda.CudaNdArray]]
    """
    params = { name: self.output[name].get_params_dict() for name in self.output }
    for h in self.hidden:
      params[h] = self.hidden[h].get_params_dict()
    return params

  def set_params_by_dict(self, params):
    """
    :type params: dict[str,dict[str,numpy.ndarray|theano.sandbox.cuda.CudaNdArray]]
    """
    for name in self.output:
      self.output[name].set_params_by_dict(params[name])
    for h in self.hidden:
      self.hidden[h].set_params_by_dict(params[h])

  def get_params_shared_flat_dict(self):
    """
    :rtype: dict[str,theano.shared]
    This will collect all vars of all layers in one dict.
    We extend the param name with our custom scheme.
    """
    params = {}
    for l_name, layer in list(self.output.items()) + list(self.hidden.items()):
      for p_name, param in layer.params.items():
        p_name = "%s.%s" % (l_name, p_name)
        assert p_name not in params
        params[p_name] = param
    return params

  def save_hdf(self, model, epoch):
    """
    :type model: h5py.File
    :type epoch: int
    """
    grp = model.create_group('training')
    model.attrs['json'] = self.json_content
    model.attrs['update_step'] = self.update_step
    model.attrs['epoch'] = epoch
    model.attrs['output'] = 'output' #self.output.keys
    model.attrs['n_in'] = self.n_in
    out = model.create_group('n_out')
    for k in self.n_out:
      out.attrs[k] = self.n_out[k][0]
    out_dim = out.create_group("dim")
    for k in self.n_out:
      out_dim.attrs[k] = self.n_out[k][1]
    for h in self.hidden:
      self.hidden[h].save(model)
    for k in self.output:
      self.output[k].save(model)

  def to_json_content(self):
    out = {}
    for name in self.output:
      out[name] = self.output[name].to_json()
    for h in self.hidden.keys():
      out[h] = self.hidden[h].to_json()
    return out

  def to_json(self):
    json_content = self.to_json_content()
    return json.dumps(json_content, sort_keys=True)

  def load_hdf(self, model):
    """
    :type model: h5py.File
    :returns last epoch this was trained on
    :rtype: int
    """
    for name in self.hidden:
      if not name in model:
        print >> log.v2, "unable to load layer", name
      else:
        self.hidden[name].load(model)
    for name in self.output:
      self.output[name].load(model)
    return self.epoch_from_hdf_model(model)

  @classmethod
  def epoch_from_hdf_model(cls, model):
    """
    :type model: h5py.File
    :returns last epoch the model was trained on
    :rtype: int
    """
    epoch = model.attrs['epoch']
    return epoch

  @classmethod
  def epoch_from_hdf_model_filename(cls, model_filename):
    """
    :type model_filename: str
    :returns last epoch the model was trained on
    :rtype: int
    """
    model = h5py.File(model_filename, "r")
    epoch = cls.epoch_from_hdf_model(model)
    model.close()
    return epoch

  def print_network_info(self, name="Network"):
    print >> log.v2, "%s layer topology:" % name
    print >> log.v2, "  input #:", self.n_in
    for layer_name, layer in sorted(self.hidden.items()):
      print >> log.v2, "  hidden %s %r #: %i" % (layer.layer_class, layer_name, layer.attrs["n_out"])
    if not self.hidden:
      print >> log.v2, "  (no hidden layers)"
    for layer_name, layer in sorted(self.output.items()):
      print >> log.v2, "  output %s %r #: %i" % (layer.layer_class, layer_name, layer.attrs["n_out"])
    if not self.output:
      print >> log.v2, "  (no output layers)"
    print >> log.v2, "net params #:", self.num_params()
    print >> log.v2, "net trainable params:", self.train_params_vars
