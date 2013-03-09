#!/usr/bin/python

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.log import setLogLevel
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.cli import CLI
from mininet.node import CPULimitedHost
from mininet.link import TCLink
from mininet.util import custom

class myTopo(Topo):

    def __init__(self, **params):

        # Initialize topology
        Topo.__init__(self, **params)

        # Create Clients
        hw = self.addHost('hw', cpu=.5, sched='cfs')
        hs = self.addHost('hs', cpu=.5, sched='cfs')
        ho1 = self.addHost('ho1', cpu=.5, sched='cfs')
        ho2 = self.addHost('ho2', cpu=.5, sched='cfs')

        # Create servers
        so = self.addHost('so', cpu=.1, sched='cfs')
        sn = self.addHost('sn', cpu=.5, sched='cfs')
        sne = self.addHost('sne', cpu=.3, sched='cfs')
        sse = self.addHost('sse', cpu=.3, sched='cfs')

        # Create switches
        switches = [self.addSwitch('s%d' % i) for i in range(1, 10)]

        # Links
        self.addLink(switches[0], hw)
        self.addLink(switches[0], ho1)
        self.addLink(switches[0], switches[1])
        self.addLink(switches[1], ho2)
        #self.addLink(switches[1], switches[2])
        self.addLink(switches[1], switches[3])
        self.addLink(switches[1], switches[4], bw=100,
                max_queue_size=10000, use_htb=True)
        #self.addLink(switches[2], switches[3])
        self.addLink(switches[2], switches[4])
        self.addLink(switches[2], switches[5])
        #self.addLink(switches[3], switches[4])
        self.addLink(switches[3], switches[6])
        self.addLink(switches[3], switches[7])
        self.addLink(switches[4], switches[8])
        self.addLink(switches[4], so)
        self.addLink(switches[5], hs)
        self.addLink(switches[6], sse)
        self.addLink(switches[7], sne)
        self.addLink(switches[8], sn)

def customNet(**kwargs):
    topo = myTopo()
    return Mininet(topo=topo, **kwargs)

def backgroundTraffic(net):
    so = net.get('so')
    ho1 = net.get('ho1')
    ho2 = net.get('ho2')

    so.cmdPrint('iperf -s -w 64K &')
    ho1.cmdPrint('iperf -c', so.IP(), '-t 3600 -w 64K -P 3 &')
    ho2.cmdPrint('iperf -c', so.IP(), '-t 3600 -w 64K -P 3 &')

def bgtShut(net):
    so = net.get('so')
    ho1 = net.get('ho1')
    ho2 = net.get('ho2')

    ho1.cmd('kill %iperf')
    ho2.cmd('kill %iperf')
    so.cmd('kill %iperf')

def concerningTraffic(net):
    hw = net.get('hw')
    hs = net.get('hs')
    servers = [net.get('sn'), net.get('sne'), net.get('sse')]

    # hs ping Servers
    print hs.name + ' PING SERVERS'
    for server in servers:
        hs.cmdPrint('ping -c4', server.IP())

    # hw ping Servers
    print hw.name + ' PING SERVERS'
    for server in servers:
        hw.cmdPrint('ping -c4', server.IP())

def cntShut(net):
    pass

if __name__ == '__main__':
    setLogLevel('debug')
    net = customNet(host=CPULimitedHost, link=TCLink,
            switch=OVSKernelSwitch,
            controller=custom(RemoteController, ip='192.168.1.126', port=6633),
            listenPort=6634)
    net.start()

    # Background traffic
    #backgroundTraffic(net)
    #concerningTraffic(net)
    CLI(net)
    #bgtShut(net)

    net.stop()
