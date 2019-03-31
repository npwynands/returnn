
"""
Provides the learning rate scheduling logic.
The base class is :class:`LearningRateControl`.
"""

from __future__ import print_function

import os
import typing
from Util import better_repr, simple_obj_repr, ObjAsDict, unicode
from Log import log
import numpy


class LearningRateControl(object):
  """
  Base class for learning rate control / scheduling.
  """

  need_error_info = True

  class EpochData:
    """
    Encapsulates all relevant information for one epoch,
    needed to perform learning rate scheduling,
    such as the individual scores (cv or train; cross-entropy or frame-error or whatever).
    """

    # Need to keep the non-PEP8 name for compatibility, because we store the repr of the object.
    # noinspection PyPep8Naming
    def __init__(self, learningRate, error=None):
      """
      :type learningRate: float
      :type error: dict[str,float] | None
      """
      self.learningRate = learningRate
      if isinstance(error, float):  # Old format.
        error = {"old_format_score": error}
      if error is None:
        error = {}
      self.error = error

    __repr__ = simple_obj_repr

  @classmethod
  def load_initial_kwargs_from_config(cls, config):
    """
    :type config: Config.Config
    :rtype: dict[str]
    """
    return {
      "defaultLearningRate": config.float('learning_rate', 1.0),
      "minLearningRate": config.float('min_learning_rate', 0.0),
      "defaultLearningRates": config.typed_value('learning_rates') or config.float_list('learning_rates'),
      "errorMeasureKey": (config.typed_value('learning_rate_control_error_measure')
                          or config.value('learning_rate_control_error_measure', None)),
      "relativeErrorAlsoRelativeToLearningRate": config.bool('learning_rate_control_relative_error_relative_lr', False),
      "minNumEpochsPerNewLearningRate": config.int("learning_rate_control_min_num_epochs_per_new_lr", 0),
      "relativeErrorDivByOld": config.bool('newbob_relative_error_div_by_old', False),
      "filename": config.value('learning_rate_file', None),
    }

  @classmethod
  def load_initial_from_config(cls, config):
    """
    :type config: Config.Config
    :rtype: LearningRateControl
    """
    kwargs = cls.load_initial_kwargs_from_config(config)
    return cls(**kwargs)

  def __init__(self, defaultLearningRate, minLearningRate=0.0, defaultLearningRates=None,
               errorMeasureKey=None,
               relativeErrorAlsoRelativeToLearningRate=False,
               minNumEpochsPerNewLearningRate=0,
               relativeErrorDivByOld=False,
               filename=None):
    """
    :param float defaultLearningRate: default learning rate. usually for epoch 1
    :param list[float] | dict[int,float] defaultLearningRates: learning rates
    :param str|list[str]|None errorMeasureKey: for getEpochErrorValue() the selector for EpochData.error which is a dict
    :param int minNumEpochsPerNewLearningRate: if the lr was recently updated, use it for at least N epochs
    :param bool relativeErrorDivByOld: if True, compute relative error as (new - old) / old.
    :param str filename: load from and save to file
    """
    self.epochData = {}  # type: typing.Dict[int,LearningRateControl.EpochData]
    self.defaultLearningRate = defaultLearningRate
    self.minLearningRate = minLearningRate
    if defaultLearningRates:
      if isinstance(defaultLearningRates, list):
        defaultLearningRates = {i + 1: v for (i, v) in enumerate(defaultLearningRates)}
      if isinstance(defaultLearningRates, (str, unicode)):
        defaultLearningRates = eval(defaultLearningRates)
      assert isinstance(defaultLearningRates, dict)
      for epoch, v in defaultLearningRates.items():
        self.set_default_learning_rate_for_epoch(epoch, v)
    self.defaultLearningRates = defaultLearningRates
    self.errorMeasureKey = errorMeasureKey
    self.relativeErrorAlsoRelativeToLearningRate = relativeErrorAlsoRelativeToLearningRate
    self.minNumEpochsPerNewLearningRate = minNumEpochsPerNewLearningRate
    self.relativeErrorDivByOld = relativeErrorDivByOld
    self.filename = filename
    if filename:
      if os.path.exists(filename):
        print("Learning-rate-control: loading file %s" % filename, file=log.v4)
        self.load()
      else:
        print("Learning-rate-control: file %s does not exist yet" % filename, file=log.v4)
    else:
      print("Learning-rate-control: no file specified, not saving history (no proper restart possible)", file=log.v4)

  __repr__ = simple_obj_repr

  def __str__(self):
    return "%r, epoch data: %s, error key: %s" % \
           (self, ", ".join(["%i: %s" % (epoch, self.epochData[epoch])
                             for epoch in sorted(self.epochData.keys())]),
            self.get_error_key(epoch=1))

  def calc_learning_rate_for_epoch(self, epoch):
    """
    :type epoch: int
    :returns learning rate
    :rtype: float
    """
    raise NotImplementedError

  def calc_new_learnign_rate_for_epoch(self, epoch):
    """
    :param int epoch:
    :return: new learning rate for this epoch
    :rtype: float
    """
    if self.minNumEpochsPerNewLearningRate > 1:
      last_lrs = [self.epochData[e].learningRate
                  for e in self._last_epochs_for_epoch(epoch, numEpochs=self.minNumEpochsPerNewLearningRate)]
      if len(set(last_lrs)) >= 2 or 0 < len(last_lrs) < self.minNumEpochsPerNewLearningRate:
        return last_lrs[-1]
    learning_rate = self.calc_learning_rate_for_epoch(epoch)
    if learning_rate < self.minLearningRate:
      return self.minLearningRate
    return learning_rate

  def _last_epochs_for_epoch(self, epoch, numEpochs):
    """
    :param int epoch:
    :param int numEpochs:
    :return: last N epochs where we have some epoch data
    :rtype: list[int]
    """
    last_epochs = sorted([e for e in self.epochData.keys() if e < epoch])
    if not last_epochs:
      return []
    last_epochs = last_epochs[-numEpochs:]
    return last_epochs

  def get_learning_rate_for_epoch(self, epoch):
    """
    :type epoch: int
    :rtype: float
    """
    assert epoch >= 1
    if epoch in self.epochData: return self.epochData[epoch].learningRate
    learning_rate = self.calc_new_learnign_rate_for_epoch(epoch)
    self.set_default_learning_rate_for_epoch(epoch, learning_rate)
    return learning_rate

  def set_default_learning_rate_for_epoch(self, epoch, learningRate):
    """
    :type epoch: int
    :type learningRate: float
    """
    if epoch in self.epochData:
      if not self.epochData[epoch].learningRate:
        self.epochData[epoch].learningRate = learningRate
    else:
      self.epochData[epoch] = self.EpochData(learningRate)

  def get_last_epoch(self, epoch):
    """
    :param int epoch:
    :return: last epoch before ``epoch`` where we have some epoch data
    :rtype: int
    """
    epochs = sorted([e for e in self.epochData.keys() if e < epoch])
    if not epochs:
      return None
    return epochs[-1]

  def get_most_recent_learning_rate(self, epoch, excludeCurrent=True):
    """
    :param int epoch:
    :param bool excludeCurrent:
    :return: most learning rate before or including ``epoch``
    :rtype: float
    """
    for e, data in reversed(sorted(self.epochData.items())):
      if e > epoch:
        continue
      if excludeCurrent and e == epoch:
        continue
      if data.learningRate is None:
        continue
      return data.learningRate
    return self.defaultLearningRate

  def calc_relative_error(self, oldEpoch, newEpoch):
    """
    :param int oldEpoch:
    :param int newEpoch:
    :return: relative error between old epoch and new epoch
    :rtype: float
    """
    old_key, old_error = self.get_epoch_error_key_value(oldEpoch)
    new_key, new_error = self.get_epoch_error_key_value(newEpoch)
    if old_error is None or new_error is None:
      return None
    if old_key != new_key:
      return None
    if self.relativeErrorDivByOld:
      relative_error = (new_error - old_error) / abs(old_error)
    else:
      relative_error = (new_error - old_error) / abs(new_error)
    if self.relativeErrorAlsoRelativeToLearningRate:
      learning_rate = self.get_most_recent_learning_rate(newEpoch, excludeCurrent=False)
      # If the learning rate is lower than the initial learning rate,
      # the relative error is also expected to be lower, so correct for that here.
      relative_error /= learning_rate / self.defaultLearningRate
    return relative_error

  def set_epoch_error(self, epoch, error):
    """
    :type epoch: int
    :type error: dict[str,float|dict[str,float]]
    """
    if epoch not in self.epochData:
      print("Learning rate not set for epoch %i. Assuming default." % epoch, file=log.v4)
      self.get_learning_rate_for_epoch(epoch)  # This will set it.
    assert isinstance(error, dict)
    error = error.copy()
    for k, v in list(error.items()):
      if isinstance(v, dict):  # like error = {"dev_score": {"cost:output1": .., "cost:output2": ...}, ...}
        del error[k]
        if len(v) == 1:
          error[k] = list(v.values())[0]
          continue
        for k1, v1 in v.items():
          if ":" in k1:
            k1 = k1[k1.index(":") + 1:]
          error[k + "_" + k1] = v1
    for v in error.values():
      assert isinstance(v, float)
    self.epochData[epoch].error.update(error)
    if epoch == 1:
      print("Learning-rate-control: error key %r from %r" % (self.get_error_key(epoch), error), file=log.v4)

  def get_error_key(self, epoch):
    """
    :param int epoch:
    :return: key which we should look in scores/errors, for this epoch
    :rtype: str
    """
    if epoch not in self.epochData:
      if isinstance(self.errorMeasureKey, list):
        return self.errorMeasureKey[0]
      assert isinstance(self.errorMeasureKey, (str, type(None)))
      return self.errorMeasureKey
    epoch_data = self.epochData[epoch]
    if not epoch_data.error:
      return None
    if len(epoch_data.error) == 1 and "old_format_score" in epoch_data.error:
      return "old_format_score"
    keys = []
    if isinstance(self.errorMeasureKey, list):
      for key in self.errorMeasureKey:
        keys += [key, key + "_output"]  # for multiple outputs, try default output
    elif isinstance(self.errorMeasureKey, str):
      keys += [self.errorMeasureKey, self.errorMeasureKey + "_output"]
    else:
      assert self.errorMeasureKey is None
    keys += ["dev_score", "dev_score_output"]
    for key in keys:
      if key in epoch_data.error:
        return key
    for key in sorted(epoch_data.error.keys()):
      if key == "dev_score_output/output" or key.startswith("dev_score_output/output_"):
        return key
    for key in sorted(epoch_data.error.keys()):
      if key.startswith("dev_score_output/"):
        return key
    for key in sorted(epoch_data.error.keys()):
      if key.startswith("dev_"):
        return key
    for key in ["train_score", "train_score_output"]:
      if key in epoch_data.error:
        return key
    return min(epoch_data.error.keys())

  def get_epoch_error_dict(self, epoch):
    """
    :param int epoch:
    :rtype: dict[str,float]
    """
    if epoch not in self.epochData:
      return {}
    return self.epochData[epoch].error

  def get_epoch_error_value(self, epoch):
    """
    :param int epoch:
    :return: error/score for the specific epoch, given the error-key, see :func:`get_error_key`
    :rtype: float
    """
    error = self.get_epoch_error_dict(epoch)
    if not error:
      return None
    key = self.get_error_key(epoch)
    assert key
    assert key in error, (
      "%r not in %r. fix %r in config. set it to %r or so." % (
        key, error, 'learning_rate_control_error_measure', 'dev_error'))
    return error[key]

  def get_epoch_error_key_value(self, epoch):
    """
    :param int epoch:
    :return: key, error
    :rtype: (str, float)
    """
    error = self.get_epoch_error_dict(epoch)
    if not error:
      return None, None
    key = self.get_error_key(epoch)
    assert key
    assert key in error, ("%r not in %r. fix %r in config. set it to %r or so." %
      (key, error, 'learning_rate_control_error_measure', 'dev_error'))
    return key, error[key]

  def get_last_best_epoch(self, last_epoch, first_epoch=1, filter_score=float("inf"), only_last_n=-1,
                          min_score_dist=0.0):
    """
    :param int first_epoch: will check all epochs >= first_epoch
    :param int last_epoch: inclusive. will check all epochs <= last_epoch
    :param float filter_score: all epochs which values over this score are not considered
    :param int only_last_n: if set (>=1), from the resulting list, we consider only the last only_last_n
    :param float min_score_dist: filter out epochs where the diff to the most recent is not big enough
    :return: the last best epoch. to get the details then, you might want to use getEpochErrorDict.
    :rtype: int|None
    """
    if first_epoch > last_epoch:
      return None
    values = [(self.get_epoch_error_key_value(ep), ep) for ep in range(first_epoch, last_epoch + 1)]
    # Note that the order of the checks here is a bit arbitrary but I had some thoughts on it.
    # Changing the order will also slightly change the behavior, so be sure it make sense.
    values = [((key, v), ep) for ((key, v), ep) in values if v is not None]
    if not values:
      return None
    last_key, latest_score = values[-1][0]
    values = [(v, ep) for ((key, v), ep) in values if key == last_key]  # only same key
    values = [(v, ep) for (v, ep) in values if v <= filter_score]
    if not values:
      return None
    if only_last_n >= 1:
      values = values[-only_last_n:]
    values = [(v, ep) for (v, ep) in values if v + min_score_dist < latest_score]
    if not values:
      return None
    return min(values)[1]

  def save(self):
    """
    Save the current epoch data to file (self.filename).
    """
    if not self.filename:
      return
    # First write to a temp-file, to be sure that the write happens without errors.
    # Otherwise, it could happen that we delete the old existing file, then
    # some error happens (e.g. disk quota), and we loose the newbob data.
    # Loosing that data is very bad because it basically means that we have to redo all the training.
    tmp_filename = self.filename + ".new_tmp"
    f = open(tmp_filename, "w")
    f.write(better_repr(self.epochData))
    f.write("\n")
    f.close()
    os.rename(tmp_filename, self.filename)

  def load(self):
    """
    Loads the saved epoch data from file (self.filename).
    """
    s = open(self.filename).read()
    self.epochData = eval(s, {"nan": float("nan"), "inf": float("inf")}, ObjAsDict(self))


class ConstantLearningRate(LearningRateControl):
  """
  Just a constant learning rate.
  """

  need_error_info = False

  def calc_learning_rate_for_epoch(self, epoch):
    """
    Dummy constant learning rate. Returns initial learning rate.
    :type epoch: int
    :returns learning rate
    :rtype: float
    """
    while True:
      last_epoch = self.get_last_epoch(epoch)
      if last_epoch is None:
        return self.defaultLearningRate
      learning_rate = self.epochData[last_epoch].learningRate
      if learning_rate is None:
        epoch = last_epoch
        continue
      return learning_rate


class NewbobRelative(LearningRateControl):
  """
  If relative diff between old and new error is over some threshold, decay learning rate.
  """

  @classmethod
  def load_initial_kwargs_from_config(cls, config):
    """
    :type config: Config.Config
    :rtype: dict[str]
    """
    kwargs = super(NewbobRelative, cls).load_initial_kwargs_from_config(config)
    kwargs.update({
      "relativeErrorThreshold": config.float('newbob_relative_error_threshold', -0.01),
      "learningRateDecayFactor": config.float('newbob_learning_rate_decay', 0.5)})
    return kwargs

  def __init__(self, relativeErrorThreshold, learningRateDecayFactor, **kwargs):
    """
    :param float defaultLearningRate: learning rate for epoch 1+2
    :type relativeErrorThreshold: float
    :type learningRateDecayFactor: float
    :type filename: str
    """
    super(NewbobRelative, self).__init__(**kwargs)
    self.relativeErrorThreshold = relativeErrorThreshold
    self.learningRateDecayFactor = learningRateDecayFactor

  def calc_learning_rate_for_epoch(self, epoch):
    """
    Newbob+ on train data.
    :type epoch: int
    :returns learning rate
    :rtype: float
    """
    last_epoch = self.get_last_epoch(epoch)
    if last_epoch is None:
      return self.defaultLearningRate
    learning_rate = self.epochData[last_epoch].learningRate
    if learning_rate is None:
      return self.defaultLearningRate
    last2_epoch = self.get_last_epoch(last_epoch)
    if last2_epoch is None:
      return learning_rate
    relative_error = self.calc_relative_error(last2_epoch, last_epoch)
    if relative_error is None:
      return learning_rate
    if relative_error > self.relativeErrorThreshold:
      learning_rate *= self.learningRateDecayFactor
    return learning_rate


class NewbobAbs(LearningRateControl):
  """
  If absolute diff between old and new error is over some threshold, decay learning rate.
  """

  @classmethod
  def load_initial_kwargs_from_config(cls, config):
    """
    :type config: Config.Config
    :rtype: dict[str]
    """
    kwargs = super(NewbobAbs, cls).load_initial_kwargs_from_config(config)
    kwargs.update({
      "errorThreshold": config.float('newbob_error_threshold', -0.01),
      "learningRateDecayFactor": config.float('newbob_learning_rate_decay', 0.5)})
    return kwargs

  def __init__(self, errorThreshold, learningRateDecayFactor, **kwargs):
    """
    :type errorThreshold: float
    :type learningRateDecayFactor: float
    """
    super(NewbobAbs, self).__init__(**kwargs)
    self.errorThreshold = errorThreshold
    self.learningRateDecayFactor = learningRateDecayFactor

  def calc_learning_rate_for_epoch(self, epoch):
    """
    Newbob+ on train data.
    :type epoch: int
    :returns learning rate
    :rtype: float
    """
    last_epoch = self.get_last_epoch(epoch)
    if last_epoch is None:
      return self.defaultLearningRate
    learning_rate = self.epochData[last_epoch].learningRate
    if learning_rate is None:
      return self.defaultLearningRate
    last2_epoch = self.get_last_epoch(last_epoch)
    if last2_epoch is None:
      return learning_rate
    old_key, old_error = self.get_epoch_error_key_value(last2_epoch)
    new_key, new_error = self.get_epoch_error_key_value(last_epoch)
    if old_error is None or new_error is None:
      return learning_rate
    if old_key != new_key:
      return learning_rate
    error_diff = new_error - old_error
    if error_diff > self.errorThreshold:
      learning_rate *= self.learningRateDecayFactor
    return learning_rate


class NewbobMultiEpoch(LearningRateControl):
  """
  Like :class:`NewbobRelative`, but looks at the average relative error over multiple epochs.
  This is useful together with ``partition_epoch`` from :class:`Dataset`.
  """

  @classmethod
  def load_initial_kwargs_from_config(cls, config):
    """
    :type config: Config.Config
    :rtype: dict[str]
    """
    kwargs = super(NewbobMultiEpoch, cls).load_initial_kwargs_from_config(config)
    kwargs.update({
      "numEpochs": config.int("newbob_multi_num_epochs", 5),
      "updateInterval": config.int("newbob_multi_update_interval", config.int("newbob_multi_num_epochs", 5)),
      "relativeErrorThreshold": config.float('newbob_relative_error_threshold', -0.01),
      "learningRateDecayFactor": config.float('newbob_learning_rate_decay', 0.5),
      "learningRateGrowthFactor": config.float('newbob_learning_rate_growth', 1.0),
      })
    return kwargs

  def __init__(self, numEpochs,  updateInterval,
               relativeErrorThreshold, learningRateDecayFactor, learningRateGrowthFactor=1.0, **kwargs):
    """
    :param float defaultLearningRate: learning rate for epoch 1+2
    :param int numEpochs:
    :param int updateInterval:
    :param float relativeErrorThreshold:
    :param float learningRateDecayFactor:
    :param int filename:
    """
    super(NewbobMultiEpoch, self).__init__(**kwargs)
    self.numEpochs = numEpochs
    assert self.numEpochs >= 1
    self.updateInterval = updateInterval
    assert self.updateInterval >= 1
    self.relativeErrorThreshold = relativeErrorThreshold
    self.learningRateDecayFactor = learningRateDecayFactor
    self.learningRateGrowthFactor = learningRateGrowthFactor

  def _calc_mean_relative_error(self, epochs):
    """
    :param list[int] epochs:
    :return: mean of relative errors
    :rtype: float|None
    """
    assert len(epochs) >= 2
    errors = [self.calc_relative_error(epochs[i], epochs[i + 1]) for i in range(len(epochs) - 1)]
    if any([e is None for e in errors]):
      return None
    return numpy.mean(errors)

  def _calc_recent_mean_relative_error(self, epoch):
    """
    :param int epoch:
    :return: recent mean of relative errors
    :rtype: float|None
    """
    # Take one more than numEpochs because we are looking at the diffs.
    last_epochs = self._last_epochs_for_epoch(epoch, numEpochs=self.numEpochs + 1)
    if not last_epochs:
      return None
    # We could also use the self.numEpochs limit here. But maybe this is better.
    if len(last_epochs) <= 1:
      return None
    return self._calc_mean_relative_error(last_epochs)

  def calc_learning_rate_for_epoch(self, epoch):
    """
    Newbob+ on train data.
    :type epoch: int
    :returns learning rate
    :rtype: float
    """
    learning_rate = self.get_most_recent_learning_rate(epoch)
    # We start counting epochs at 1.
    if self.updateInterval > 1 and epoch % self.updateInterval != 1:
      return learning_rate
    mean_relative_error = self._calc_recent_mean_relative_error(epoch)
    if mean_relative_error is None:
      return learning_rate
    if mean_relative_error > self.relativeErrorThreshold:
      learning_rate *= self.learningRateDecayFactor
    else:
      learning_rate *= self.learningRateGrowthFactor
    return learning_rate


def learning_rate_control_type(type_name):
  """
  :param str type_name:
  :rtype: type[LearningRateControl]|LearningRateControl
  """
  if type_name == "constant":
    return ConstantLearningRate
  elif type_name in ("newbob", "newbob_rel", "newbob_relative"):  # Old setups expect the relative version.
    return NewbobRelative
  elif type_name == "newbob_abs":
    return NewbobAbs
  elif type_name == "newbob_multi_epoch":
    return NewbobMultiEpoch
  else:
    assert False, "unknown learning-rate-control type %s" % type_name


def load_learning_rate_control_from_config(config):
  """
  :type config: Config.Config
  :rtype: LearningRateControl
  """
  control_type = config.value("learning_rate_control", "constant")
  cls = learning_rate_control_type(control_type)
  return cls.load_initial_from_config(config)


def demo():
  """
  Demo run. Given some learning rate file (with scores / existing lrs), will calculate how lrs would have been set,
  given some config.
  """
  import better_exchook
  better_exchook.install()
  import rnn
  import sys
  if len(sys.argv) <= 1:
    print("usage: python %s [config] [other options] [++check_learning_rates 1]" % __file__)
    print(
      ("example usage: "
       "python %s ++learning_rate_control newbob ++learning_rate_file newbob.data ++learning_rate 0.001") % __file__)
  rnn.init_config(command_line_options=sys.argv[1:])
  # noinspection PyProtectedMember
  rnn.config._hack_value_reading_debug()
  rnn.config.update({"log": []})
  rnn.init_log()
  rnn.init_backend_engine()
  check_lr = rnn.config.bool("check_learning_rates", False)
  from Pretrain import pretrain_from_config
  pretrain = pretrain_from_config(rnn.config)
  first_non_pretrain_epoch = 1
  pretrain_learning_rate = None
  if pretrain:
    first_non_pretrain_epoch = pretrain.get_train_num_epochs() + 1
  log.initialize(verbosity=[5])
  control = load_learning_rate_control_from_config(rnn.config)
  print("LearningRateControl: %r" % control)
  if not control.epochData:
    print("No epoch data so far.")
    return
  first_epoch = min(control.epochData.keys())
  if first_epoch != 1:
    print("Strange, first epoch from epoch data is %i." % first_epoch)
  print("Error key: %s from %r" % (control.get_error_key(epoch=first_epoch), control.epochData[first_epoch].error))
  if pretrain:
    pretrain_learning_rate = rnn.config.float('pretrain_learning_rate', control.defaultLearningRate)
  max_epoch = max(control.epochData.keys())
  for epoch in range(1, max_epoch + 2):  # all epochs [1..max_epoch+1]
    old_learning_rate = None
    if epoch in control.epochData:
      old_learning_rate = control.epochData[epoch].learningRate
    if epoch < first_non_pretrain_epoch:
      learning_rate = pretrain_learning_rate
      s = "Pretrain epoch %i, fixed learning rate: %s (was: %s)" % (epoch, learning_rate, old_learning_rate)
    elif 1 < first_non_pretrain_epoch == epoch:
      learning_rate = control.defaultLearningRate
      s = "First epoch after pretrain, epoch %i, fixed learning rate: %s (was %s)" % (
        epoch, learning_rate, old_learning_rate)
    else:
      learning_rate = control.calc_new_learnign_rate_for_epoch(epoch)
      s = "Calculated learning rate for epoch %i: %s (was: %s)" % (epoch, learning_rate, old_learning_rate)
    if learning_rate < control.minLearningRate:
      learning_rate = control.minLearningRate
      s += ", clipped to %s" % learning_rate
    s += ", previous relative error: %s" % control.calc_relative_error(epoch - 2, epoch - 1)
    if hasattr(control, "_calc_recent_mean_relative_error"):
      # noinspection PyProtectedMember
      s += ", previous mean relative error: %s" % control._calc_recent_mean_relative_error(epoch)
    print(s)
    if check_lr and old_learning_rate is not None:
      if old_learning_rate != learning_rate:
        print("Learning rate is different in epoch %i!" % epoch)
        sys.exit(1)
    # Overwrite new learning rate so that the calculation for further learning rates stays consistent.
    if epoch in control.epochData:
      control.epochData[epoch].learningRate = learning_rate
    else:
      control.epochData[epoch] = control.EpochData(learningRate=learning_rate)
  print("Finished, last stored epoch was %i." % max_epoch)


if __name__ == "__main__":
  demo()
