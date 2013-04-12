from pox.core import core
import pox
log = core.getLogger()

from pox.lib.util import dpid_to_str
from pox.lib.revent import *
from pox.lib.recoco import Timer
from pox.openflow.of_json import *

import pox.openflow.libopenflow_01 as of

from collections import defaultdict
import time

class StatsReplyCounter (object):
  def __init__ (self):
    self.cnt = 0
    self.timer = None

  def req_sent (self):
    self.cnt += 1

  def req_received (self):
    self.cnt -= 1
    if self.cnt == 0:
      log.debug("StatsRequest have been replied. \
                Raising LinkStatsUpdated event...")
      core.openflow_link_monitor.raiseEvent(LinkStatsUpdated())

  def _stats_reply_timeout (self):
    if self.cnt > 0:
      log.debug("%i stats are not replied. \
                Anyway, raising LinkStatsUpdated event...", self.cnt)
      self.cnt = 0
      core.openflow_link_monitor.raiseEvent(LinkStatsUpdated())

  def clear (self):
    self.cnt = 0
    self.timer = Timer(3, self._stats_reply_timeout)

cnt = StatsReplyCounter()

def _periodic_port_stats_req ():
  psq = of.ofp_port_stats_request(port_no = of.OFPP_NONE)
  msg = of.ofp_stats_request(body = psq)

  cnt.clear()
  con = None
  for con in core.openflow.connections:
    con.send(msg)
    cnt.req_sent()

class LinkStatsUpdated (Event):
  """
  Link statisctics for all switch ports are updated.
  """
  def __init__ (self):
    Event.__init__(self)

class LinkMonitor (EventMixin):

  _eventMixin_events = set([
    LinkStatsUpdated,
  ])

  _core_name = "openflow_link_monitor"

  def __init__ (self):
    core.openflow.addListeners(self)
    self._port_stats = defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:0.0)))
    self.link_util = defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:0.0)))

  def _handle_ConnectionUp (self, event):
    for port in event.connection.ports:
      if port < 48:
        if (self.link_util[event.dpid][port]['rx'] != self._port_stats[event.dpid][port]['rx']
            or self.link_util[event.dpid][port]['tx'] != self._port_stats[event.dpid][port]['tx']):
          log.debug('_link_util != _port_stats')

  def _handle_PortStatsReceived (self, event):
    stats = flow_stats_to_list(event.stats)
    dpid = event.connection.dpid
    for stat in stats:
      port_no = stat['port_no']
      # filtering out the local port number 65534
      if port_no < 48:
        # Calculate link utilization
        self.link_util[dpid][port_no]['rx'] = (stat['rx_bytes'] - self._port_stats[dpid][port_no]['rx']) * 8.0 / (5.0 * 1000000.0)
        self.link_util[dpid][port_no]['tx'] = (stat['tx_bytes'] - self._port_stats[dpid][port_no]['tx']) * 8.0 / (5.0 * 1000000.0)
        #log.debug("%s %i RX capacity used %.7f", dpid_to_str(dpid), port_no, self.link_util[dpid][port_no]['rx'])
        #log.debug("%s %i TX capacity used %.7f", dpid_to_str(dpid), port_no, self.link_util[dpid][port_no]['tx'])
        # Update port stats
        self._port_stats[dpid][port_no]['rx'] = stat['rx_bytes']
        self._port_stats[dpid][port_no]['tx'] = stat['tx_bytes']
    cnt.req_received()

def launch ():
  core.registerNew(LinkMonitor)
  Timer(5, _periodic_port_stats_req, recurring=True)
