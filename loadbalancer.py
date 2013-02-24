#!/usr/bin/python

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.log import setLogLevel
from mininet.node import OVSKernelSwitch
from mininet.cli import CLI

class myTopo(Topo):

    def __init__(self, **params):

        # Initialize topology
        Topo.__init__(self, **params)

        # Create Clients
        h_w = self.addHost('h-w')
        h_s = self.addHost('h-s')
        h_o1 = self.addHost('h-o1')
        h_o2 = self.addHost('h-o2')

        # Create servers
        s_o = self.addHost('s-o')
        s_n = self.addHost('s-n')
        s_ne = self.addHost('s-ne')
        s_se = self.addHost('s-se')

        # Create switches
        switches = [self.addSwitch('s%d' % i) for i in range(1, 10)]

        # Links
        self.addLink(switches[0], h_w)
        self.addLink(switches[0], h_o1)
        self.addLink(switches[0], switches[1])
        self.addLink(switches[1], h_o2)
        #self.addLink(switches[1], switches[2])
        self.addLink(switches[1], switches[3])
        self.addLink(switches[1], switches[4])
        #self.addLink(switches[2], switches[3])
        self.addLink(switches[2], switches[4])
        self.addLink(switches[2], switches[5])
        #self.addLink(switches[3], switches[4])
        self.addLink(switches[3], switches[6])
        self.addLink(switches[3], switches[7])
        self.addLink(switches[4], switches[8])
        self.addLink(switches[4], s_o)
        self.addLink(switches[5], h_s)
        self.addLink(switches[6], s_se)
        self.addLink(switches[7], s_ne)
        self.addLink(switches[8], s_n)

if __name__ == '__main__':
    setLogLevel('info')
    topo = myTopo()
    net = Mininet(topo=topo, switch=OVSKernelSwitch)
    net.start()
    net.pingAll()
    CLI(net)
    net.stop()

