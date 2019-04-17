#!/usr/bin/env python
"""Cron management classes."""
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import logging
import sys
import threading
import time


from future.utils import iterkeys

from grr_response_core import config
from grr_response_core.lib import rdfvalue
from grr_response_core.lib import registry
from grr_response_core.lib.rdfvalues import protodict as rdf_protodict
from grr_response_core.lib.util import compatibility
from grr_response_core.lib.util import random
from grr_response_core.stats import stats_collector_instance
from grr_response_server import access_control
from grr_response_server import aff4
from grr_response_server import cronjobs
from grr_response_server import data_store
from grr_response_server import flow
from grr_response_server import queue_manager
from grr_response_server.rdfvalues import cronjobs as rdf_cronjobs
from grr_response_server.rdfvalues import flow_runner as rdf_flow_runner
from grr_response_server.rdfvalues import hunts as rdf_hunts


class Error(Exception):
  pass


class CronManager(object):
  """CronManager is used to schedule/terminate cron jobs."""

  CRON_JOBS_PATH = rdfvalue.RDFURN("aff4:/cron")

  def CreateJob(self, cron_args=None, job_id=None, token=None, enabled=True):
    """Creates a cron job that runs given flow with a given frequency.

    Args:
      cron_args: A protobuf of type rdf_cronjobs.CreateCronJobArgs.
      job_id: Use this job_id instead of an autogenerated unique name (used for
        system cron jobs - we want them to have well-defined persistent name).
      token: Security token used for data store access.
      enabled: If False, the job object will be created, but will be disabled.

    Returns:
      Name of the cron job created.
    """
    if not job_id:
      uid = random.UInt16()
      job_id = "%s_%s" % (cron_args.flow_name, uid)

    flow_runner_args = rdf_flow_runner.FlowRunnerArgs(
        flow_name="CreateAndRunGenericHuntFlow")

    flow_args = rdf_hunts.CreateGenericHuntFlowArgs()
    flow_args.hunt_args.flow_args = cron_args.flow_args
    flow_args.hunt_args.flow_runner_args.flow_name = cron_args.flow_name
    flow_args.hunt_runner_args = cron_args.hunt_runner_args
    flow_args.hunt_runner_args.hunt_name = "GenericHunt"

    create_cron_args = rdf_cronjobs.CreateCronJobFlowArgs(
        description=cron_args.description,
        periodicity=cron_args.frequency,
        flow_runner_args=flow_runner_args,
        flow_args=flow_args,
        allow_overruns=cron_args.allow_overruns,
        lifetime=cron_args.lifetime)

    cron_job_urn = self.CRON_JOBS_PATH.Add(job_id)
    with aff4.FACTORY.Create(
        cron_job_urn,
        aff4_type=CronJob,
        mode="rw",
        token=token,
        force_new_version=False) as cron_job:

      # If the cronjob was already present we don't want to overwrite the
      # original start_time.
      existing_cron_args = cron_job.Get(cron_job.Schema.CRON_ARGS)
      if existing_cron_args and existing_cron_args.start_time:
        create_cron_args.start_time = existing_cron_args.start_time

      if create_cron_args != existing_cron_args:
        cron_job.Set(cron_job.Schema.CRON_ARGS(create_cron_args))

      cron_job.Set(cron_job.Schema.DISABLED(not enabled))

    return job_id

  def ListJobs(self, token=None):
    """Returns a list of all currently running cron jobs."""
    job_root = aff4.FACTORY.Open(self.CRON_JOBS_PATH, token=token)
    return [urn.Basename() for urn in job_root.ListChildren()]

  def ReadJob(self, job_id, token=None):
    job_urn = self.CRON_JOBS_PATH.Add(job_id)
    return aff4.FACTORY.Open(
        job_urn, aff4_type=CronJob, token=token, age=aff4.ALL_TIMES)

  def ReadJobs(self, token=None):
    job_urns = [self.CRON_JOBS_PATH.Add(job_id) for job_id in self.ListJobs()]
    return aff4.FACTORY.MultiOpen(
        job_urns, aff4_type=CronJob, token=token, age=aff4.ALL_TIMES)

  def ReadJobRuns(self, job_id, token=None):
    job_urn = self.CRON_JOBS_PATH.Add(job_id)
    fd = aff4.FACTORY.Open(job_urn, token=token)
    return list(fd.OpenChildren())

  def EnableJob(self, job_id, token=None):
    """Enable cron job with the given URN."""
    job_urn = self.CRON_JOBS_PATH.Add(job_id)
    cron_job = aff4.FACTORY.Open(
        job_urn, mode="rw", aff4_type=CronJob, token=token)
    cron_job.Set(cron_job.Schema.DISABLED(0))
    cron_job.Close()

  def DisableJob(self, job_id, token=None):
    """Disable cron job with the given URN."""
    job_urn = self.CRON_JOBS_PATH.Add(job_id)
    cron_job = aff4.FACTORY.Open(
        job_urn, mode="rw", aff4_type=CronJob, token=token)
    cron_job.Set(cron_job.Schema.DISABLED(1))
    cron_job.Close()

  def DeleteJob(self, job_id, token=None):
    """Deletes cron job with the given URN."""
    job_urn = self.CRON_JOBS_PATH.Add(job_id)
    aff4.FACTORY.Delete(job_urn, token=token)

  def RunOnce(self, token=None, force=False, names=None):
    """Tries to lock and run cron jobs.

    Args:
      token: security token
      force: If True, force a run
      names: List of job names to run.  If unset, run them all
    """
    names = names or self.ListJobs(token=token)
    urns = [self.CRON_JOBS_PATH.Add(name) for name in names]

    for cron_job_urn in urns:
      try:
        with aff4.FACTORY.OpenWithLock(
            cron_job_urn, blocking=False, token=token,
            lease_time=600) as cron_job:
          try:
            logging.info("Running cron job: %s", cron_job.urn)
            cron_job.Run(force=force)
          except Exception as e:  # pylint: disable=broad-except
            logging.exception("Error processing cron job %s: %s", cron_job.urn,
                              e)
            stats_collector_instance.Get().IncrementCounter(
                "cron_internal_error")

      except aff4.LockError:
        pass

  def DeleteOldRuns(self, job, cutoff_timestamp=None, token=None):
    """Deletes flows initiated by the job that are older than specified."""
    if cutoff_timestamp is None:
      raise ValueError("cutoff_timestamp can't be None")

    child_flows = list(job.ListChildren(age=cutoff_timestamp))
    with queue_manager.QueueManager(token=token) as queuemanager:
      queuemanager.MultiDestroyFlowStates(child_flows)

    aff4.FACTORY.MultiDelete(child_flows, token=token)
    return len(child_flows)


def GetCronManager():
  if data_store.RelationalDBEnabled():
    return cronjobs.CronManager()
  return CronManager()


class SystemCronFlow(flow.GRRFlow):
  """SystemCronFlows are scheduled automatically on workers startup."""

  frequency = rdfvalue.Duration("1d")
  lifetime = rdfvalue.Duration("20h")
  allow_overruns = False

  # Jobs that are broken, or are under development can be disabled using
  # the "enabled" attribute. These jobs won't get scheduled automatically,
  # and will get paused if they were scheduled before.
  enabled = True

  __abstract = True  # pylint: disable=g-bad-name

  def _ValidateState(self):
    # For normal flows it's a bug to write an empty state, here it's ok.
    pass

  @property
  def disabled(self):
    raise ValueError("Disabled flag is deprecated, use enabled instead.")

  @disabled.setter
  def disabled(self, _):
    raise ValueError("Disabled flag is deprecated, use enabled instead.")


class StateReadError(Error):
  pass


class StateWriteError(Error):
  pass


class StatefulSystemCronFlow(SystemCronFlow):
  """SystemCronFlow that keeps a permanent state between iterations."""

  __abstract = True

  @property
  def cron_job_urn(self):
    return CronManager.CRON_JOBS_PATH.Add(self.__class__.__name__)

  def ReadCronState(self):
    # TODO(amoser): This is pretty bad, there is no locking for state.
    try:
      cron_job = aff4.FACTORY.Open(
          self.cron_job_urn, aff4_type=CronJob, token=self.token)
      res = cron_job.Get(cron_job.Schema.STATE_DICT)
      if res:
        return flow.AttributedDict(res.ToDict())
      return flow.AttributedDict()
    except aff4.InstantiationError as e:
      raise StateReadError(e)

  def WriteCronState(self, state):
    if not state:
      return

    try:
      with aff4.FACTORY.OpenWithLock(
          self.cron_job_urn, aff4_type=CronJob, token=self.token) as cron_job:
        cron_job.Set(cron_job.Schema.STATE_DICT(state))
    except aff4.InstantiationError as e:
      raise StateWriteError(e)


def ScheduleSystemCronFlows(names=None, token=None):
  """Schedule all the SystemCronFlows found."""

  if data_store.RelationalDBEnabled():
    return cronjobs.ScheduleSystemCronJobs(names=names)

  errors = []
  for name in config.CONFIG["Cron.disabled_system_jobs"]:
    try:
      cls = registry.AFF4FlowRegistry.FlowClassByName(name)
    except ValueError:
      errors.append("No such flow: %s." % name)
      continue

    if not issubclass(cls, SystemCronFlow):
      errors.append("Disabled system cron job name doesn't correspond to "
                    "a flow inherited from SystemCronFlow: %s" % name)

  if names is None:
    names = iterkeys(registry.AFF4FlowRegistry.FLOW_REGISTRY)

  for name in names:
    cls = registry.AFF4FlowRegistry.FlowClassByName(name)

    if not issubclass(cls, SystemCronFlow):
      continue

    cron_args = rdf_cronjobs.CreateCronJobFlowArgs(
        periodicity=cls.frequency,
        lifetime=cls.lifetime,
        allow_overruns=cls.allow_overruns)
    cron_args.flow_runner_args.flow_name = name

    if cls.enabled:
      enabled = name not in config.CONFIG["Cron.disabled_system_jobs"]
    else:
      enabled = False

    job_urn = CronManager.CRON_JOBS_PATH.Add(name)
    with aff4.FACTORY.Create(
        job_urn,
        aff4_type=CronJob,
        mode="rw",
        token=token,
        force_new_version=False) as cron_job:

      # If the cronjob was already present we don't want to overwrite the
      # original start_time.
      existing_cron_args = cron_job.Get(cron_job.Schema.CRON_ARGS)

      if cron_args != existing_cron_args:
        cron_job.Set(cron_job.Schema.CRON_ARGS(cron_args))

      cron_job.Set(cron_job.Schema.DISABLED(not enabled))

  if errors:
    raise ValueError("Error(s) while parsing Cron.disabled_system_jobs: %s" %
                     errors)


class CronWorker(object):
  """CronWorker runs a thread that periodically executes cron jobs."""

  def __init__(self, thread_name="grr_cron", sleep=60 * 5):
    self.thread_name = thread_name
    self.sleep = sleep

    # SetUID is required to write cronjobs under aff4:/cron/
    self.token = access_control.ACLToken(
        username="GRRCron", reason="Implied.").SetUID()

  def _RunLoop(self):
    ScheduleSystemCronFlows(token=self.token)

    while True:
      try:
        GetCronManager().RunOnce(token=self.token)
      except Exception as e:  # pylint: disable=broad-except
        logging.error("CronWorker uncaught exception: %s", e)

      time.sleep(self.sleep)

  def Run(self):
    """Runs a working thread and waits for it to finish."""
    self.RunAsync().join()

  def RunAsync(self):
    """Runs a working thread and returns immediately."""
    self.running_thread = threading.Thread(
        name=self.thread_name, target=self._RunLoop)
    self.running_thread.daemon = True
    self.running_thread.start()
    return self.running_thread


class CronJob(aff4.AFF4Volume):
  """AFF4 object corresponding to cron jobs."""

  class SchemaCls(aff4.AFF4Volume.SchemaCls):
    """Schema for CronJob AFF4 object."""
    CRON_ARGS = aff4.Attribute("aff4:cron/args",
                               rdf_cronjobs.CreateCronJobFlowArgs,
                               "This cron jobs' arguments.")

    DISABLED = aff4.Attribute(
        "aff4:cron/disabled",
        rdfvalue.RDFBool,
        "If True, don't run this job.",
        versioned=False)

    CURRENT_FLOW_URN = aff4.Attribute(
        "aff4:cron/current_flow_urn",
        rdfvalue.RDFURN,
        "URN of the currently running flow corresponding to this cron job.",
        versioned=False,
        lock_protected=True)

    LAST_RUN_TIME = aff4.Attribute(
        "aff4:cron/last_run",
        rdfvalue.RDFDatetime,
        "The last time this cron job ran.",
        "last_run",
        versioned=False,
        lock_protected=True)

    LAST_RUN_STATUS = aff4.Attribute(
        "aff4:cron/last_run_status",
        rdf_cronjobs.CronJobRunStatus,
        "Result of the last flow",
        lock_protected=True,
        creates_new_object_version=False)

    STATE_DICT = aff4.Attribute(
        "aff4:cron/state_dict",
        rdf_protodict.AttributedDict,
        "Cron flow state that is kept between iterations",
        lock_protected=True,
        versioned=False)

  def IsRunning(self):
    """Returns True if there's a currently running iteration of this job."""
    current_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if not current_urn:
      return False

    try:
      current_flow = aff4.FACTORY.Open(
          urn=current_urn, aff4_type=flow.GRRFlow, token=self.token, mode="r")
    except aff4.InstantiationError:
      # This isn't a flow, something went really wrong, clear it out.
      logging.error("Unable to open cron job run: %s", current_urn)
      self.DeleteAttribute(self.Schema.CURRENT_FLOW_URN)
      self.Flush()
      return False

    return current_flow.GetRunner().IsRunning()

  def DueToRun(self):
    """Called periodically by the cron daemon, if True Run() will be called.

    Returns:
        True if it is time to run based on the specified frequency.
    """
    if self.Get(self.Schema.DISABLED):
      return False

    cron_args = self.Get(self.Schema.CRON_ARGS)
    last_run_time = self.Get(self.Schema.LAST_RUN_TIME)
    now = rdfvalue.RDFDatetime.Now()

    # Its time to run.
    if (last_run_time is None or
        now > cron_args.periodicity.Expiry(last_run_time)):

      # Not due to start yet.
      if now < cron_args.start_time:
        return False

      # Do we allow overruns?
      if cron_args.allow_overruns:
        return True

      # No currently executing job - lets go.
      if self.Get(self.Schema.CURRENT_FLOW_URN) is None:
        return True

    return False

  def StopCurrentRun(self, reason="Cron lifetime exceeded."):
    current_flow_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if current_flow_urn:
      flow.GRRFlow.TerminateAFF4Flow(
          current_flow_urn, reason=reason, token=self.token)
      self.Set(
          self.Schema.LAST_RUN_STATUS,
          rdf_cronjobs.CronJobRunStatus(
              status=rdf_cronjobs.CronJobRunStatus.Status.TIMEOUT))
      self.DeleteAttribute(self.Schema.CURRENT_FLOW_URN)
      self.Flush()

  def KillOldFlows(self):
    """Disable cron flow if it has exceeded CRON_ARGS.lifetime.

    Returns:
      bool: True if the flow is was killed.
    """
    if not self.IsRunning():
      return False

    start_time = self.Get(self.Schema.LAST_RUN_TIME)
    lifetime = self.Get(self.Schema.CRON_ARGS).lifetime

    elapsed = rdfvalue.RDFDatetime.Now() - start_time

    if lifetime and elapsed > lifetime:
      self.StopCurrentRun()
      stats_collector_instance.Get().IncrementCounter(
          "cron_job_timeout", fields=[self.urn.Basename()])
      stats_collector_instance.Get().RecordEvent(
          "cron_job_latency", elapsed.seconds, fields=[self.urn.Basename()])
      return True

    return False

  def Run(self, force=False):
    """Do the actual work of the Cron.

    Will first check if DueToRun is True.

    CronJob object must be locked (i.e. opened via OpenWithLock) for Run() to be
    called.

    Args:
      force: If True, the job will run no matter what (i.e. even if DueToRun()
        returns False).

    Raises:
      LockError: if the object is not locked.
    """
    if not self.locked:
      raise aff4.LockError("CronJob must be locked for Run() to be called.")

    self.KillOldFlows()

    # If currently running flow has finished, update our state.
    current_flow_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if current_flow_urn:
      current_flow = aff4.FACTORY.Open(current_flow_urn, token=self.token)
      runner = current_flow.GetRunner()
      if not runner.IsRunning():
        if runner.context.state == rdf_flow_runner.FlowContext.State.ERROR:
          self.Set(
              self.Schema.LAST_RUN_STATUS,
              rdf_cronjobs.CronJobRunStatus(
                  status=rdf_cronjobs.CronJobRunStatus.Status.ERROR))
          stats_collector_instance.Get().IncrementCounter(
              "cron_job_failure", fields=[self.urn.Basename()])
        else:
          self.Set(
              self.Schema.LAST_RUN_STATUS,
              rdf_cronjobs.CronJobRunStatus(
                  status=rdf_cronjobs.CronJobRunStatus.Status.OK))

          start_time = self.Get(self.Schema.LAST_RUN_TIME)
          elapsed = time.time() - start_time.AsSecondsSinceEpoch()
          stats_collector_instance.Get().RecordEvent(
              "cron_job_latency", elapsed, fields=[self.urn.Basename()])

        self.DeleteAttribute(self.Schema.CURRENT_FLOW_URN)
        self.Flush()

    if not force and not self.DueToRun():
      return

    # Make sure the flow is created with cron job as a parent folder.
    cron_args = self.Get(self.Schema.CRON_ARGS)
    cron_args.flow_runner_args.base_session_id = self.urn

    flow_urn = flow.StartAFF4Flow(
        runner_args=cron_args.flow_runner_args,
        args=cron_args.flow_args,
        token=self.token,
        sync=False)

    self.Set(self.Schema.CURRENT_FLOW_URN, flow_urn)
    self.Set(self.Schema.LAST_RUN_TIME, rdfvalue.RDFDatetime.Now())
    self.Flush()


class CronHook(registry.InitHook):
  """Init hook for cron job metrics."""

  pre = [aff4.AFF4InitHook]

  def RunOnce(self):
    """Main CronHook method."""
    # Start the cron thread if configured to.
    if config.CONFIG["Cron.active"]:

      self.cron_worker = CronWorker()
      self.cron_worker.RunAsync()


class LegacyCronJobAdapterMixin(object):
  """Mixin used by DualDBSystemCronJob decorator to generate legacy classes."""

  def Start(self):
    self.Run()


def DualDBSystemCronJob(legacy_name=None, stateful=False):
  """Decorator that creates AFF4 and RELDB cronjobs from a given mixin."""

  def Decorator(cls):
    """Decorator producing 2 classes: legacy style one and a new style one."""
    if not legacy_name:
      raise ValueError("legacy_name has to be provided")

    # Legacy cron jobs have different base classes depending on whether they're
    # stateful or not.
    if stateful:
      aff4_base_cls = StatefulSystemCronFlow
    else:
      aff4_base_cls = SystemCronFlow

    # Make sure that we're dealing with a true mixin to avoid subtle errors.
    if issubclass(cls, cronjobs.SystemCronJobBase):
      raise ValueError("Mixin class shouldn't inherit from SystemCronJobBase")

    if issubclass(cls, aff4_base_cls):
      raise ValueError("Mixin class shouldn't inherit from %s" %
                       aff4_base_cls.__name__)

    # Generate legacy class. Register it within the module as it's not going
    # to be returned from the decorator.
    aff4_cls = compatibility.MakeType(
        legacy_name, (cls, LegacyCronJobAdapterMixin, aff4_base_cls), {})
    module = sys.modules[cls.__module__]
    setattr(module, legacy_name, aff4_cls)

    # Generate new class. No need to register it in the module (like the legacy
    # one) since it will replace the original decorated class.
    reldb_cls = compatibility.MakeType(
        compatibility.GetName(cls), (cls, cronjobs.SystemCronJobBase), {})
    return reldb_cls

  return Decorator
