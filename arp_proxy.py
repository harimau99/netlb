# Copyright 2011,2012 James McCauley
#
# This file is part of POX.
#
# POX is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# POX is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with POX.  If not, see <http://www.gnu.org/licenses/>.

"""
An ARP utility that can learn and proxy ARPs, and can also answer queries
from a list of static entries.

This adds the "arp" object to the console, which you can use to look at
or modify the ARP table.
"""

from pox.core import core
import pox
log = core.getLogger()

from pox.lib.packet.ethernet import ethernet, ETHER_ANY, ETHER_BROADCAST
from pox.lib.packet import arp, ipv4
from pox.lib.addresses import IPAddr, EthAddr, IP_ANY
from pox.lib.util import dpid_to_str, str_to_bool
from pox.lib.recoco import Timer
from pox.lib.revent import EventHalt

import pox.openflow.libopenflow_01 as of

import time


# Timeout for ARP entries
ARP_TIMEOUT = 60 * 4


class Entry (object):
  """
  We use the MAC to answer ARP replies.
  We use the timeout so that if an entry is older than ARP_TIMEOUT, we
   flood the ARP request rather than try to answer it ourselves.
  """
  def __init__ (self, mac, static = False):
    self.timeout = time.time() + ARP_TIMEOUT
    self.static = static
    if mac is True:
      # Means use switch's MAC, implies True
      self.mac = True
      self.static = True
    else:
      self.mac = EthAddr(mac)

  def __eq__ (self, other):
    if isinstance(other, Entry):
      return (self.static,self.mac)==(other.static,other.mac)
    else:
      return self.mac == other
  def __ne__ (self, other):
    return not self.__eq__(other)

  def __str__ (self):
    return str(self.mac)

  @property
  def is_expired (self):
    if self.static: return False
    return time.time() > self.timeout


class ARPTable (dict):
  def __repr__ (self):
    o = []
    for k,e in self.iteritems():
      t = int(e.timeout - time.time())
      if t < 0:
        t = "X"
      else:
        t = str(t) + "s left"
      if e.static: t = "-"
      mac = e.mac
      if mac is True: mac = "<Switch MAC>"
      o.append((k,"%-17s %-20s %3s" % (k, mac, t)))

    for k,t in _failed_queries.iteritems():
      if k not in self:
        t = int(time.time() - t)
        o.append((k,"%-17s %-20s %3ss ago" % (k, '?', t)))

    o.sort()
    o = [e[1] for e in o]
    o.insert(0,"-- ARP Table -----")
    if len(o) == 1:
      o.append("<< Empty >>")
    return "\n".join(o)

  def __setitem__ (self, key, val):
    key = IPAddr(key)
    if not isinstance(val, Entry):
      val = Entry(val)
    dict.__setitem__(self, key, val)

  def __delitem__ (self, key):
    key = IPAddr(key)
    dict.__delitem__(self, key)

  def set (self, key, value=True, static=True):
    if not isinstance(value, Entry):
      value = Entry(value, static=static)
    self[key] = value


def _dpid_to_mac (dpid):
  # Should maybe look at internal port MAC instead?
  return EthAddr("%012x" % (dpid & 0xffFFffFFffFF,))


def _handle_expiration ():
  for k,e in _arp_table.items():
    if e.is_expired:
      del _arp_table[k]
  for k,t in _failed_queries.items():
    if time.time() - t > ARP_TIMEOUT:
      del _failed_queries[k]

def _pick_real_server ():
  # FIXME need more work here
  for real_server in _real_servers:
    return real_server

def _arp_for_real_servers ():
  for rs in [server for server in _real_servers
             if server not in _arp_table]:
    log.debug("ARPing in UpEvent for real server %s", str(rs))
    q = arp()
    q.opcode = arp.REQUEST
    q.protodst = rs

    con = None
    for con in core.openflow.connections:
      if con is not None:
        break
    else:
      log.info("can't get any connection")
      return True

    e = ethernet(type=ethernet.ARP_TYPE, dst=ETHER_BROADCAST)
    e.payload = q
    log.debug("%s ARPing in UpEvent for real server %s",
              dpid_to_str(con.dpid), str(rs))
    msg = of.ofp_packet_out()
    msg.data = e.pack()
    msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
    # FIXME assume port number starts from 1
    # There will be a problem if port 1 is the
    # only port that will join flooding (spanning_tree)
    msg.in_port = 1
    con.send(msg)

  res = False
  for server in _real_servers:
    if server not in _arp_table:
      res = True
      break
  return res

class ARPResponder (object):
  def __init__ (self):
    # This timer handles expiring stuff
    self._expire_timer = Timer(5, _handle_expiration, recurring=True)
    self._arp_timer = Timer(3, _arp_for_real_servers, recurring=True,
                            selfStoppable=True)

    core.addListeners(self)

  def _handle_GoingUpEvent (self, event):
    core.openflow.addListeners(self)
    log.debug("Up...")
    # We are expecting at least one real server if there is one or more
    # virtual servers
    if len(_virtual_servers) > 0 and len(_real_servers) == 0:
      log.info("Do you miss real server configuration?")

  def _handle_ConnectionUp (self, event):
    if _install_flow:
      fm = of.ofp_flow_mod()
      fm.priority = 0x7000 # Pretty high
      fm.match.dl_type = ethernet.ARP_TYPE
      fm.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
      event.connection.send(fm)

  def _handle_PacketIn (self, event):
    # Note: arp.hwsrc is not necessarily equal to ethernet.src
    # (one such example are arp replies generated by this module itself
    # as ethernet mac is set to switch dpid) so we should be careful
    # to use only arp addresses in the learning code!
    squelch = False

    dpid = event.connection.dpid
    inport = event.port
    packet = event.parsed
    if not packet.parsed:
      log.warning("%s: ignoring unparsed packet", dpid_to_str(dpid))
      return

    if isinstance(packet.next, arp):
      a = packet.next

      log.debug("%s ARP %s %s => %s", dpid_to_str(dpid),
        {arp.REQUEST:"request",arp.REPLY:"reply"}.get(a.opcode,
        'op:%i' % (a.opcode,)), str(a.protosrc), str(a.protodst))

      if a.prototype == arp.PROTO_TYPE_IP:
        if a.hwtype == arp.HW_TYPE_ETHERNET:
          if a.protosrc != 0:

            if _learn:
              # Learn or update port/MAC info
              if a.protosrc in _arp_table:
                if _arp_table[a.protosrc] != a.hwsrc:
                  log.warn("%s RE-learned %s: was at %s->now at %s",
                           (dpid_to_str(dpid), a.protosrc,
                            _arp_table[a.protosrc], a.hwsrc))
              else:
                log.info("%s learned %s is at %s", dpid_to_str(dpid),
                         a.protosrc, a.hwsrc)

              if a.protosrc not in _real_servers:
                _arp_table[a.protosrc] = Entry(a.hwsrc)
              else:
                # Static ARP entries for real servers
                _arp_table.set(a.protosrc, a.hwsrc, static=True)


            if a.opcode == arp.REQUEST:
              # Maybe we can answer

              if a.protodst in _arp_table or a.protodst in _virtual_servers:
                # We have an answer...

                r = arp()
                r.hwtype = a.hwtype
                r.prototype = a.prototype
                r.hwlen = a.hwlen
                r.protolen = a.protolen
                r.opcode = arp.REPLY
                r.hwdst = a.hwsrc
                r.protodst = a.protosrc
                r.protosrc = a.protodst

                # We use L2 routing. For each ARP request for a virtual server, we reply it
                # with MAC address of a real server
                if a.protodst in _virtual_servers:
                  server_ip = _pick_real_server()
                  # We assume that each real server has a static ARP entry.
                  mac = _arp_table[server_ip].mac
                  log.info("ARP request for VS %s, answering with %s at %s", a.protodst,
                          server_ip, mac)
                else:
                  mac = _arp_table[a.protodst].mac
                  if mac is True:
                    # Special case -- use ourself
                    mac = _dpid_to_mac(dpid)

                r.hwsrc = mac
                e = ethernet(type=packet.type, src=_dpid_to_mac(dpid),
                              dst=a.hwsrc)
                e.payload = r
                log.info("%s answering ARP for %s" % (dpid_to_str(dpid),
                  str(r.protosrc)))
                msg = of.ofp_packet_out()
                msg.data = e.pack()
                msg.actions.append(of.ofp_action_output(port =
                                                        of.OFPP_IN_PORT))
                msg.in_port = inport
                event.connection.send(msg)
                return EventHalt if _eat_packets else None
              else:
                # Keep track of failed queries
                squelch = a.protodst in _failed_queries
                _failed_queries[a.protodst] = time.time()

      """
      if a.opcode == arp.REPLY:
        msg = "%s flooding ARP %s %s => %s" % (dpid_to_str(dpid),
            {arp.REQUEST:"request",arp.REPLY:"reply"}.get(a.opcode,
            'op:%i' % (a.opcode,)), a.protosrc, a.protodst)

        if squelch:
          log.debug(msg)
        else:
          log.info(msg)

        msg = of.ofp_packet_out()
        msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
        msg.data = event.ofp
        event.connection.send(msg.pack())
        return EventHalt if _eat_packets else None
      else:
      """
      # This ARP Proxy learns addresses, and replies to ARPs that it can answer.
      # However it is not supposed to flood any ARP packets . The forwarding
      # engine will do that.

      # What's the difference between returning EventHalt/ND None? None
      # means this event is not handled by this listener. Other listeners
      # should continue processing it. Otherwise, the event is finished.
      return None

    elif isinstance(packet.next, ipv4):
      # For now, mac_map in forwarding/l2_multi.py is static. It might change
      # when hosts move around. But it won't timeout. As long as two hosts have
      # exchanged ARP packets, their locations will be learned by the controller.
      # Other flows between them will go through the data plane by setting up
      # 'paths'. So the ARP Proxy will NOT be able to intercept these packets
      # and update their ARP entries.
      ip = packet.next
      log.debug("%s IP %s => %s only for address learning", dpid_to_str(dpid),
                ip.srcip, ip.dstip)

      if _learn:
        if ip.srcip in _arp_table:
          if _arp_table[ip.srcip] != packet.src:
            log.warn("%s RE-learned %s: was at %s->now at %s",
              dpid_to_str(dpid), ip.srcip, _arp_table[ip.srcip], packet.src)
        else:
          log.info("%s learned %s is at %s", dpid_to_str(dpid), ip.srcip,
            packet.src)
          _arp_table[ip.srcip] = Entry(packet.src)
      # This ipv4 packet is only for ARP learning, will not process it.
      return None

_arp_table = ARPTable() # IPAddr -> Entry
_install_flow = None
_eat_packets = None
_failed_queries = {} # IP -> time : queries we couldn't answer
_learn = None
_virtual_servers = None
_real_servers = set()

# I changed the default value of no_flow to True. Thus the ARP->Controller
# flow will not be installed. ARP replies can/ND should be correctly
# 'routed' by the forwarding engine.
def launch (timeout=ARP_TIMEOUT, no_flow=True, eat_packets=True,
            no_learn=False, virtual_servers="", real_servers=""):
  global ARP_TIMEOUT, _install_flow, _eat_packets, _learn, \
      _virtual_servers, _real_servers
  ARP_TIMEOUT = timeout
  _install_flow = not no_flow
  _eat_packets = str_to_bool(eat_packets)
  _learn = not no_learn
  _virtual_servers = set([IPAddr(k) for k in
                          virtual_servers.replace(",", " ").split()])

  core.Interactive.variables['arp'] = _arp_table
  _real_servers.update([IPAddr(k) for k in
                        real_servers.replace(",", " ").split()])
  core.registerNew(ARPResponder)

