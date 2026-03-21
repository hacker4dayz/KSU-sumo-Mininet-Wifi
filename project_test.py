#!/usr/bin/env python3

from mininet.log import setLogLevel, info
from mn_wifi.cli import CLI
from mn_wifi.net import Mininet_wifi
from mn_wifi.sumo.runner import sumo
from mn_wifi.link import wmediumd, ITSLink
from mn_wifi.wmediumdConnector import interference
from mn_wifi.telemetry import telemetry 
from mininet.node import Controller, RemoteController
import subprocess
import os

def topology():
    net = Mininet_wifi(link=wmediumd, wmediumd_mode=interference)

    print("*** Starting...")

    print("*** Adding nodes...")

    # 12 Normal cars
    cars = []
    for x in range(0,12):
        cars.append(
        net.addCar('car%s' % (x+1), 
                   wlans=2)
        )
        
    # Emergency vehicles (police + ambulance)
    police = net.addCar('police',
                        wlans=2)
    ambulance = net.addCar('ambulance',
                           wlans=2)
    
    police.params['priority'] = 'high'
    ambulance.params['priority'] = 'high'
    for car in cars:  
        car.params['priority'] = 'normal'
    print("*** Priority: police/high, ambulance/high, others/normal")

    kwargs = {'ssid': 'roadside-ssid', 
              'mode': 'g',
              'datapath': 'user'}
    ap1 = net.addAccessPoint('ap1', channel='1',
                             position='900,830,0', **kwargs)
    ap2 = net.addAccessPoint('ap2', channel='6',
                             position='2000,100,0', **kwargs)
    ap3 = net.addAccessPoint('ap3', channel='11',
                             position='750,1400,0', **kwargs)
    ap4 = net.addAccessPoint('ap4', channel='1',
                             position='705,265,0', **kwargs)
    ap5 = net.addAccessPoint('ap5', channel='6',
                             position='1635,1265,0', **kwargs)

    server = net.addHost('server', ip='10.0.0.100/24') 
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    print("*** Configuring Propagation Model")
    net.setPropagationModel(model="logDistance", exp=2.8)

    print("*** Configuring nodes")
    net.configureWifiNodes()
  
    # AP backbone links (your tree topology)
    print("*** Creating AP backbone links...")
    net.addLink(ap1, c0)
    net.addLink(ap1, ap2)
    net.addLink(ap1, ap3)
    net.addLink(ap1, ap4)
    net.addLink(ap1, ap5)
    net.addLink(ap1, server)

    # ITSLink for DSRC (wlan1)
    print("*** Adding ITSLinks (DSRC channel 181)...")
    for car in net.cars:
        net.addLink(car, intf=car.wintfs[1].name,
                    cls=ITSLink, band=20, channel=181)

    # Start SUMO
    print("*** Starting SUMO simulation...")
    for node in net.stations:
        if node.name not in [f'car{i}' for i in range(15)]:
            node.hide()

    net.useExternalProgram(
        program=sumo,    
        port=8813,    
        config_file='ksuroadtest.sumocfg', 
        extra_params=["--start", "--delay", "1000"], 
        clients=1,
        exec_order=0
    )

    print("*** Starting network")
    net.build()
    
    print("*** Configuring AP wlan0 IPs...")
    ap_ips = ['10.0.0.101', '10.0.0.102', '10.0.0.103', '10.0.0.104', '10.0.0.105']
    for i, ap in enumerate(net.aps):
        wlan1_name = f'{ap.name}-wlan1'
        if hasattr(ap, 'wintfs') and wlan1_name in [intf.name for intf in ap.wintfs.values()]:
            ap.setIP(ap_ips[i] + '/24', intf=wlan1_name)
            print(f"  {ap.name}-wlan0: {ap_ips[i]}")

    # Start APs standalone
    for ap in net.aps:
        ap.start([c0])

    # Dual IP config (wlan0=AP WiFi, wlan1=DSRC)
    print("*** Configuring cars (dual IP)...")
    for id, car in enumerate(net.cars):
        # wlan0 = WPA2 AP connection
        car.setIP(f'10.0.0.{id+1}/24', intf=car.wintfs[0].name)
        # wlan1 = DSRC ITS backhaul
        car.setIP(f'10.0.1.{id+1}/24', intf=car.wintfs[1].name)
        print(f"  {car.name}: wlan0=10.0.0.{id+1}, wlan1=10.0.1.{id+1}")
    
    # *** FFmpeg Test NO Controller ***
   # import time
   # print("*** FFmpeg: NO Controller Test ***")
   # server.cmd('ffmpeg -re -i /dev/zero -f lavfi -f mpegts udp://224.0.0.1:1234 -t 10 -y /tmp/nocontroller.mp4 &')
   # time.sleep(12)

    print("*** Plotting Telemetry...")
    # Telemetry cars + APs
    nodes = net.cars + net.aps
    telemetry(nodes=nodes, data_type='position',
              min_x=-1200, min_y=-1500,
              max_x=4000, max_y=3000)
    
    print("*** Starting CLI (type exit to quit)...")
    CLI(net)

    print("*** Stopping network...")
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    topology()
