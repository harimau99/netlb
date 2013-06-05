# Copyright 2013 James McCauley
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pox.core import core
import pox
log = core.getLogger()

from pox.lib.packet.ethernet import ethernet, ETHER_BROADCAST
from pox.lib.packet.arp import arp
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.revent import EventHalt

import pox.openflow.libopenflow_01 as of

import time
import copy

def dpid_to_mac (dpid):
    return EthAddr("%012x" % (dpid & 0xffFFffFFffFF,))

#class ServerLoad (object):
    #"""
    #"""
    #def __init__ (self, ip, cpu, disk, mem, proc):
        #self.server = IPAddr(ip)
        #self.cpu = cpu
        #self.disk = disk
        #self.memory = mem
        #self.process = proc

def server_picker (real_servers): # generator
    servers = copy.deepcopy(real_servers)
    while True:
        for i in range(0, len(servers)):
            yield servers[i]

WAIT_FOR_STP = 60

class ServerScheduler (object):
    """
    """

    _core_name = "ssched"

    def __init__ (self, servers = []):
        self.servers = [IPAddr(x) for x in servers]
        self.live_servers = {} # IP -> MAC,dpid,port
        # IP -> load
        #self.servers_load = dict([(x, Load(IPAddr(x))) for x in servers])

        self.probe_cycle_time = 5

        self.arp_timeout = 3

        self.ongoing_probes = {} # IP -> expire_time

        #self.service_ip = IPAddr(service_ip)
        self.mac = None
        self.con = None
        self.picker = server_picker(self.servers)

        core.openflow.addListeners(self)
        core.callDelayed(WAIT_FOR_STP, self._do_probe)

    def _do_probe (self):
        self._do_expire()

        if self.con is None:
        # connectino not ready
            core.callDelayed(self._probe_wait_time, self._do_probe)
            return

        server = self.servers.pop(0)
        self.servers.append(server)

        r = arp()
        r.hwtype = r.HW_TYPE_ETHERNET
        r.prototype = r.PROTO_TYPE_IP
        r.opcode = r.REQUEST
        r.protodst = server
        #r.protosrc = self.service_ip
        r.hwdst = ETHER_BROADCAST
        r.hwsrc = dpid_to_mac(self.con.dpid)
        e = ethernet(type=ethernet.ARP_TYPE, src=dpid_to_mac(self.con.dpid),
                dst=ETHER_BROADCAST)
        e.set_payload(r)
        #log.debug("ARPing for %s", server)
        msg = of.ofp_packet_out()
        msg.data = e.pack()
        msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
        msg.in_port = of.OFPP_NONE
        self.con.send(msg)

        self.ongoing_probes[server] = time.time() + self.arp_timeout
        core.callDelayed(self._probe_wait_time, self._do_probe)

    @property
    def _probe_wait_time (self):
        r = self.probe_cycle_time / float(len(self.servers))
        r = max(.25, r)
        return r

    def _do_expire (self):
        t = time.time()

        for server, timeout in self.ongoing_probes.items():
            if t > timeout:
                self.ongoing_probes.pop(server, None)
                if server in self.live_servers:
                    log.warn("Server %s down", server)
                    del self.live_servers[server]

        # TODO flows
    def get_real_server (self):
        """
        """
        if len(self.live_servers) == 0:
            log.warn("no server available")
            return None

        server = self.picker.next()
        while server not in self.live_servers:
            server = self.picker.next()

        log.info("real server: %s, %s", server, self.live_servers[server][0])
        return server, self.live_servers[server][0]

    def _handle_PacketIn (self, event):
        # only handles ARP reply
        dpid = event.dpid
        inport = event.port
        packet = event.parsed

        arpp = packet.find('arp')
        if arpp:
            if arpp.opcode == arpp.REPLY:
                if arpp.protosrc in self.ongoing_probes:
                    del self.ongoing_probes[arpp.protosrc]
                    if (self.live_servers.get(arpp.protosrc, (None,None))
                            == (arpp.hwsrc,dpid,inport)):
                        pass
                    else:
                        self.live_servers[arpp.protosrc] = arpp.hwsrc,dpid,inport
                        log.info("Server %s up", arpp.protosrc)
                    return # EventHalt
        return

    def _handle_ConnectionUp (self, event):
        if self.con is None:
            self.con = event.connection

def launch (servers):
    core.registerNew(ServerScheduler, servers.replace(',',' ').split())
