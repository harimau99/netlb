from pox.core import core
import pox
log = core.getLogger()

from pox.lib.revent import *
from pox.lib.recoco import Timer
from pox.openflow.of_json import *
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str

from collections import defaultdict
import time

class StatsReplyCounter (object):
    def __init__ (self, reply_timeout):
        self.cnt = 0
        self.timer = None
        self._timeout = reply_timeout

    def req_sent (self):
        self.cnt += 1

    def req_received (self):
        self.cnt -= 1
        if self.cnt == 0:
          log.debug("StatsRequest have been replied. \
                    Raising LinkStatsUpdated event...")
          core.lblm.raiseEvent(LinkStatsUpdated())

    def _stats_reply_timeout (self):
        if self.cnt > 0:
          log.debug("%i stats are not replied. \
                    Anyway, raising LinkStatsUpdated event...", self.cnt)
          self.cnt = 0
          core.lblm.raiseEvent(LinkStatsUpdated())

    def clear (self):
        self.cnt = 0
        # all stats request sent, start timer
        self.timer = Timer(self._timeout, self._stats_reply_timeout)

class LinkStatsUpdated (Event):
  """
  Link statisctics for all switches are updated.
  """
  def __init__ (self):
    Event.__init__(self)

class LinkMonitor (EventMixin):
    """
    """
    _eventMixin_events = set([
      LinkStatsUpdated,
    ])

    _core_name = "lblm"

    def __init__ (self, cycle_time, bandwidth):
        core.openflow.addListeners(self)
        self._stats_request_cycle_time = cycle_time
        self._stats_request_timeout = cycle_time - 2.0
        self.bandwidth = float(bandwidth) * 1000000.0

        self._port_stats = defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:0.0)))
        self.link_util = defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:0.0)))

        self._memory = {} # dpid -> timestamp
        self.counter = StatsReplyCounter(self._stats_request_timeout)

        self._do_request()

    def _do_request (self):
        psq = of.ofp_port_stats_request(port_no = of.OFPP_NONE)
        msg = of.ofp_stats_request(body = psq)
        t = time.time()

        self.counter.clear()
        con = None
        if len(core.openflow.connections) == 0:
            log.warn("No connections")
        else:
            for con in core.openflow.connections:
                self._memory[con.dpid] = t
                con.send(msg)
                self.counter.req_sent()

        core.callDelayed(self._stats_request_cycle_time, self._do_request)

    def _handle_ConnectionUp (self, event):
        for port in event.connection.ports:
            if port < 48: # exclude local port 65534
                self.link_util[event.dpid][port]['rx'] = 0.0
                self.link_util[event.dpid][port]['tx'] = 0.0
                self._port_stats[event.dpid][port]['rx'] = 0.0
                self._port_stats[event.dpid][port]['tx'] = 0.0
                log.info('port and link stats initialized')

    def _handle_PortStatsReceived (self, event):
        stats = flow_stats_to_list(event.stats)
        dpid = event.connection.dpid

        t = time.time()
        #stamp = self._memory[dpid]
        #delta = t - stamp
        self._memory[dpid] = t

        for stat in stats:
          port_no = stat['port_no']
          # filtering out the local port number 65534
          if port_no < 48:
            # Calculate link utilization
            self.link_util[dpid][port_no]['rx'] = (stat['rx_bytes'] - 
                    self._port_stats[dpid][port_no]['rx']) * 8.0 / (self._stats_request_cycle_time * self.bandwidth) + .0000001
            self.link_util[dpid][port_no]['tx'] = (stat['tx_bytes'] -
                    self._port_stats[dpid][port_no]['tx']) * 8.0 / (self._stats_request_cycle_time * self.bandwidth) + .0000001
            if self.link_util[dpid][port_no]['rx'] < .000005:
                self.link_util[dpid][port_no]['rx'] = .000005
            if self.link_util[dpid][port_no]['tx'] < .000005:
                self.link_util[dpid][port_no]['tx'] = .000005
            #log.debug("%s %i RX %.7f", dpid_to_str(dpid), port_no, self.link_util[dpid][port_no]['rx'])
            #log.debug("%s %i TX %.7f", dpid_to_str(dpid), port_no, self.link_util[dpid][port_no]['tx'])
            self._port_stats[dpid][port_no]['rx'] = stat['rx_bytes']
            self._port_stats[dpid][port_no]['tx'] = stat['tx_bytes']
        self.counter.req_received()

STATS_REQUEST_CYCLE = 10

def launch (cycle_time=STATS_REQUEST_CYCLE, bandwidth=50):
    cycle_time = int(cycle_time)
    bandwidth = int(bandwidth)
    core.registerNew(LinkMonitor, cycle_time, bandwidth)
