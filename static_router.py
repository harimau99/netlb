import time

from pox.core import core
log=core.getLogger()

from pox.lib.packet.ethernet import ethernet, ETHER_BROADCAST, ETHER_ANY
from pox.lib.packet.arp import arp
from pox.lib.packet.ipv4 import ipv4
import pox.lib.packet as pkt
from pox.lib.addresses import IPAddr, EthAddr
from pox.openflow.libopenflow_01 import OFPP_LOCAL, OFPP_IN_PORT, OFPP_ALL
from pox.lib.util import dpid_to_str

import pox.openflow.libopenflow_01 as of

configs = {1 : 
           {'ip': '10.0.1.1', 
            'subnet': '10.0.1.0/24',
            'nexthop': '10.0.2.1',
            'default': 3,
            'routes' : [('10.0.1.2', 1), ('10.0.1.3', 2), ('10.0.2.1', 3)]
           }, 
           2 : 
           {'ip': '10.0.2.1',
            'subnet': '10.0.2.0/24',
            'nexthop': '10.0.1.1',
            'default': 1,
            'routes' : [('10.0.2.2', 2), ('10.0.1.1', 1)]
           }
          }


class StaticRouter(object):
    """
    StaticRouter configurations are hard-coded.
    """
    def __init__(self, dpid, connection, ofr):
        # Static configurations
        self.ip = IPAddr(configs[dpid].get('ip'))
        self.subnet = configs[dpid].get('subnet')
        self.nextHopIp = IPAddr(configs[dpid].get('nexthop'))
        self.defaultPort = configs[dpid].get('default')
        self.routes = dict([(IPAddr(ip), port_no)for ip, port_no
                            in configs[dpid].get('routes')])
        # Dynamic configurations
        self.connection = connection
        self.ports = dict([(port.port_no, port.hw_addr) for port in ofr.ports 
                           if port.port_no != OFPP_LOCAL ])
        # Layer 2 Table
        self.macToPort = {}
        # ARP table, map IP to MAC
        self.arpTable = {}
        # buffered packet, map IP to buffer_id and in_port
        self.bufferedPacket = {}
        # Listen to PacketIn messages
        connection.addListeners(self)
        # Don't have send ARP request on startup
        #time.sleep(1)
        #self.arp_to_neighbour()

    def resend_packet (self, packet_in, out_port):
        """
        Instructs the switch to resend a packet that it had sent to us.
        "packet_in" is the ofp_packet_in object the switch had sent to the
        controller due to a table-miss.
        """
        msg = of.ofp_packet_out()
        msg.data = packet_in
        # Add an action to send to the specified port
        action = of.ofp_action_output(port = out_port)
        msg.actions.append(action)
        # Send message to switch
        self.connection.send(msg)

    def flood(self, packet_in):
        log.debug("flooding caused by a packet_in from port %d" % packet_in.in_port)
        self.resend_packet(packet_in, OFPP_ALL)
 
    def act_like_switch(self, packet, packet_in):
        if packet.dst in self.macToPort:
            log.debug("Installing flow in_port=%i,dl_type=%x,dl_src=%s,dl_dst=%s,\
                      action=output:%i" % (packet_in.in_port, packet.type,
                      packet.src, packet.dst, self.macToPort[packet.dst]))
            msg = of.ofp_flow_mod()
            ## Set fields to match received packet
            msg.match = of.ofp_match.from_packet(packet)
            msg.idle_timeout = 60
            msg.buffer_id = packet_in.buffer_id
            action = of.ofp_action_output(port = self.macToPort[packet.dst])
            msg.actions.append(action)
            self.connection.send(msg)
        else:
            self.flood(packet_in)

    def sendARPrequest(self, dstip):
        request = arp()
        request.hwtype = arp.HW_TYPE_ETHERNET
        request.prototype = arp.PROTO_TYPE_IP
        request.hwlen = 6
        request.protolen = 4
        request.opcode = arp.REQUEST
        request.hwdst = ETHER_ANY
        request.protodst = dstip
        request.hwsrc = self.ports[self.routes[dstip]]
        request.protosrc = self.ip
        e = ethernet(type=ethernet.ARP_TYPE,
                     src=self.ports[self.routes[dstip]],
                     dst=ETHER_BROADCAST)
        e.set_payload(request)
        log.debug("%s ARPing for %s" % (str(self.ip), str(dstip)))
        msg = of.ofp_packet_out()
        msg.data = e.pack()
        msg.actions.append(of.ofp_action_output(port = self.routes[dstip]))
        self.connection.send(msg)

    def in_subnet(self, packet, packet_in):
        """
        packet is destined for the subnet whose gateway is this 'router'.
        1. If the packet is LAN traffic(both srcip and dstip are in the
        subnet), the 'router' just acts like a switch;
        2. If the packet comes from the WAN interface(the srcip is not
        int the subnet), we modify the src and dst(MAC address). 
        Add a flow entry with composition actoins.
        """

        # packets from outside the subnet but to the subnet
        if packet.next.srcip.inNetwork(self.subnet):
            self.act_like_switch(packet, packet_in)    
        else:
            if packet.next.dstip in self.arpTable:
                msg = of.ofp_flow_mod()
                msg.match = of.ofp_match.from_packet(packet)
                msg.idle_timeout = 60
                msg.buffer_id = packet_in.buffer_id
                msg.actions.append(of.ofp_action_dl_addr.set_src(self.ports[self.routes[packet.next.dstip]]))
                msg.actions.append(of.ofp_action_dl_addr.set_dst(self.arpTable[packet.next.dstip]))
                msg.actions.append(of.ofp_action_output(port = self.routes[packet.next.dstip]))
                self.connection.send(msg)
            else:
                if packet.next.dstip not in self.routes:
                    return
                if packet.next.dstip not in self.bufferedPacket:
                  self.bufferedPacket[packet.next.dstip] = []
                bucket = self.bufferedPacket[packet.next.dstip]
                bucket.append((packet, packet_in))
                self.sendARPrequest(packet.next.dstip)

    def to_remote(self, packet, packet_in):
        """
        Packet is forwarded to the gateway. Change the source/destination
        MAC address.
        """
        if self.nextHopIp in self.arpTable:
            log.debug("%s Installing flow in_port=%i,dl_type=%04x,dl_src=%s,"
                      "dl_dst=%s, nw_src=%s,nw_dst=%s,action=mod_dl_src:%s,"
                      "mod_dl_dst:%s,output:%i" % 
                      (str(self.ip), packet_in.in_port,packet.type,
                       str(packet.src), str(packet.dst),
                       str(packet.next.srcip), str(packet.next.dstip),
                       self.ports[self.defaultPort],
                       self.arpTable[self.nextHopIp], self.defaultPort))
            msg = of.ofp_flow_mod()
            msg.match = of.ofp_match.from_packet(packet)
            msg.idle_timeout = 60
            msg.buffer_id = packet_in.buffer_id
            msg.actions.append(of.ofp_action_dl_addr.set_src(self.ports[self.defaultPort]))
            msg.actions.append(of.ofp_action_dl_addr.set_dst(self.arpTable[self.nextHopIp]))
            msg.actions.append(of.ofp_action_output(port = self.defaultPort))
            self.connection.send(msg)
        else:
            if self.nextHopIp not in self.bufferedPacket:
                self.bufferedPacket[self.nextHopIp] = []
            bucket = self.bufferedPacket[self.nextHopIp]
            bucket.append((packet, packet_in))
            self.sendARPrequest(self.nextHopIp)
 
    def arp_to_neighbour(self):
        request = arp()
        request.hwtype = arp.HW_TYPE_ETHERNET
        request.prototype = arp.PROTO_TYPE_IP
        request.hwlen = 6
        request.protolen = 4
        request.opcode = arp.REQUEST
        request.hwdst = ETHER_ANY
        request.protodst = self.nextHopIp
        request.hwsrc = self.ports[self.defaultPort]
        request.protosrc = self.ip
        e = ethernet(type=ethernet.ARP_TYPE, src = self.ports[self.defaultPort],
                     dst = ETHER_BROADCAST)
        e.set_payload(request)
        log.debug("%s ARPing for %s" % (str(self.ip), str(self.nextHopIp)))
        msg = of.ofp_packet_out()
        msg.data = e.pack()
        msg.actions.append(of.ofp_action_output(port = self.defaultPort))
        self.connection.send(msg)

    def send_buffered_packet(self, dstip, dst):
        if dstip in self.bufferedPacket:
            bucket = self.bufferedPacket[dstip]
            del self.bufferedPacket[dstip]
            for packet, packet_in in bucket:
                msg = of.ofp_flow_mod()
                msg.match = of.ofp_match.from_packet(packet)
                msg.idle_timeout = 60
                msg.buffer_id = packet_in.buffer_id
                msg.actions.append(of.ofp_action_dl_addr.set_src(self.ports[self.routes[dstip]]))
                msg.actions.append(of.ofp_action_dl_addr.set_dst(dst))
                msg.actions.append(of.ofp_action_output(port = self.routes[dstip]))
                self.connection.send(msg)

    def _handle_PacketIn(self, event):
        packet = event.parsed
        packet_in = event.ofp

        log.debug("%i %i FRAME %s => %s", event.dpid, event.port,
                  str(packet.src), str(packet.dst))

        # mac address learning
        self.macToPort[packet.src] = event.port

        if isinstance(packet.next, ipv4):
            log.debug("%i %i IPv4 %s => %s", event.dpid, event.port,
                      str(packet.next.srcip), str(packet.next.dstip))

            # Reply to pings: 
            # 1. reply pings to routers
            # 2. reply pings to hosts that are unreachable
            if packet.find("icmp"):
                if packet.next.dstip == self.ip:
                    # Make the ping reply
                    icmp = pkt.icmp()
                    icmp.type = pkt.TYPE_ECHO_REPLY
                    icmp.payload = packet.find("icmp").payload

                    # Make the IP packet around it
                    ipp = pkt.ipv4()
                    ipp.protocol = ipv4.ICMP_PROTOCOL
                    ipp.srcip = packet.find("ipv4").dstip
                    ipp.dstip = packet.find("ipv4").srcip

                    # Ethernet around that...
                    e = pkt.ethernet()
                    e.src = packet.dst
                    e.dst = packet.src
                    e.type = ethernet.IP_TYPE

                    # Hook them up...
                    ipp.payload = icmp
                    e.payload = ipp

                    # Send it back to the input port
                    msg = of.ofp_packet_out()
                    msg.actions.append(of.ofp_action_output(port = OFPP_IN_PORT))
                    msg.data = e.pack()
                    msg.in_port = event.port
                    event.connection.send(msg)
                else:
                    if ((not packet.next.srcip.inNetwork(self.subnet)) and
                    ((not packet.next.dstip.inNetwork(self.subnet)) or
                     (packet.next.dstip not in self.routes))):
                        # Router will reply ICMP Destination Unreachable
                        unreach = pkt.unreach()
                        origi_ip = packet.find("ipv4")
                        d = origi_ip.pack()
                        d = d[:origi_ip.hl * 4 + 8]
                        unreach.payload = d

                        # Make the ping reply
                        icmp = pkt.icmp()
                        icmp.type = pkt.TYPE_DEST_UNREACH
                        icmp.code = pkt.CODE_UNREACH_HOST
                        icmp.payload = unreach

                        # Make the IP packet around it
                        ipp = pkt.ipv4()
                        ipp.protocol = ipp.ICMP_PROTOCOL
                        ipp.srcip = packet.find("ipv4").dstip
                        ipp.dstip = packet.find("ipv4").srcip

                        # Ethernet around that...
                        e = pkt.ethernet()
                        e.src = packet.dst
                        e.dst = packet.src
                        e.type = e.IP_TYPE

                        # Hook them up...
                        ipp.payload = icmp
                        e.payload = ipp

                        # Send it back to the input port
                        msg = of.ofp_packet_out()
                        msg.actions.append(of.ofp_action_output(port = of.OFPP_IN_PORT))
                        msg.data = e.pack()
                        msg.in_port = event.port
                        event.connection.send(msg)

            if packet.next.dstip.inNetwork(self.subnet):
                self.in_subnet(packet, packet_in)
            else:
                self.to_remote(packet, packet_in)
        elif isinstance(packet.next, arp):
            a = packet.next

            log.debug("%i %i ARP %s %s => %s", event.dpid, event.port,
                      {arp.REQUEST:"request",arp.REPLY:"reply"}.get(a.opcode, 'op:%i' % (a.opcode,)),
                      str(a.protosrc), str(a.protodst))

            # arp learning
            self.arpTable[a.protosrc] = a.hwsrc
            # send packets (if there are any) buffered for this ip address
            self.send_buffered_packet(a.protosrc, a.hwsrc)

            if (a.opcode == arp.REQUEST):
                # arp request for router itself
                if (a.protodst == self.ip):
                    reply = arp()
                    reply.hwtype = a.hwtype
                    reply.prototype = a.prototype
                    reply.hwlen = a.hwlen
                    reply.protolen = a.protolen
                    reply.opcode = arp.REPLY
                    reply.hwdst = a.hwsrc
                    reply.protodst = a.protosrc
                    reply.hwsrc = self.ports[event.port]
                    reply.protosrc = a.protodst
                    e = ethernet(type=packet.type, src=self.ports[event.port], dst=a.hwsrc)
                    e.set_payload(reply)
                    msg = of.ofp_packet_out()
                    msg.data = e.pack()
                    msg.actions.append(of.ofp_action_output(port = 
                                                            OFPP_IN_PORT))
                    msg.in_port = event.port
                    self.connection.send(msg)
                # arp request for a host in the subnet
                else:
                    if a.protodst in self.routes:
                        msg = of.ofp_packet_out()
                        msg.data = packet_in
                        msg.actions.append(of.ofp_action_output(port = self.routes.get(a.protodst)))
                        self.connection.send(msg)
            elif (a.opcode == arp.REPLY):
                if (a.protodst != self.ip):
                    msg = of.ofp_packet_out()
                    msg.data = packet_in
                    msg.actions.append(of.ofp_action_output(port = self.routes.get(a.protodst)))
                    self.connection.send(msg)

class L3Routing(object):
    """
    Instantitate a StaticRouter for each OpenFlow switch.
    """
    def __init__(self):
        core.openflow.addListeners(self)
    def _handle_ConnectionUp(self, event):
        log.debug("ConnectionUP %d" % event.dpid)
        StaticRouter(event.dpid, event.connection, event.ofp)

def launch ():
    core.registerNew(L3Routing)
