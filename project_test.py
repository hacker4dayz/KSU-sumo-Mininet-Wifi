#!/usr/bin/env python3

from mininet.log import setLogLevel, info
from mn_wifi.cli import CLI
from mn_wifi.net import Mininet_wifi
from mn_wifi.sumo.runner import sumo
from mn_wifi.link import wmediumd, ITSLink
from mn_wifi.wmediumdConnector import interference
from mn_wifi.telemetry import telemetry 
from mininet.node import Controller, RemoteController
import os
import threading
import time
from time import strftime
import subprocess

SHARED_DIR = os.path.abspath('.')
VIDEO_FILE = 'highway_mountain.mp4'
STREAM_PORT = 5000

def ensure_dir(d):
    os.makedirs(d, exist_ok=True)

def prepare_video():
    video_path = os.path.join(SHARED_DIR, VIDEO_FILE)
    if not os.path.exists(video_path):
        print(f"*** ERROR: {VIDEO_FILE} not found!")
        return False
    print(f"*** Video ready: {video_path}")
    return True

# Association logger -------------------------------------------------------------------
def get_ap_wlan0_addr(ap):
    intf = ap.wintfs[0].name
    out = ap.cmd(f"iw dev {intf} info")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('addr '):
            return line.split()[1].strip().lower()
    out2 = ap.cmd(f"ip link show {intf}")
    for line in out2.splitlines():
        if 'link/ether' in line:
            return line.split()[1].strip().lower()
    return None

def build_bssid_map(net):
    bmap = {}
    for ap in net.aps:
        mac = get_ap_wlan0_addr(ap)
        if mac:
            bmap[mac] = ap.name
    return bmap

def _get_assoc_info_fast(car):
    intf = car.wintfs[0].name
    out = car.cmd(f"iw dev {intf} link")
    info_dict = {'ssid': None, 'bssid': None, 'signal': None, 'raw': out}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('SSID:'):
            info_dict['ssid'] = line.split('SSID:')[1].strip()
        if line.startswith('Connected to'):
            parts = line.split()
            if len(parts) >= 3:
                info_dict['bssid'] = parts[2].strip().lower()
        if line.startswith('signal:'):
            info_dict['signal'] = line.split('signal:')[1].strip()
    return info_dict

def start_assoc_logger_fast(net, interval=0.8, timeout=60, csv=None, rebuild_map_every=8):
    if csv:
        ensure_dir(os.path.dirname(csv) if os.path.dirname(csv) else '.')

    stop_event = threading.Event()
    bssid_map = build_bssid_map(net)

    def logger():
        start_time = time.time()
        iteration = 0
        if csv:
            with open(csv, 'w') as f:   # recreate CSV at start
                f.write('timestamp,car,ssid,bssid,ap,signal\n')

        while not stop_event.is_set():
            iteration += 1
            if rebuild_map_every and iteration % rebuild_map_every == 0:
                bssid_map.clear()
                bssid_map.update(build_bssid_map(net))

            ts = strftime('%Y-%m-%d %H:%M:%S')
            info(f"*** Association snapshot {strftime('%H:%M:%S')}\n")

            all_associated = True
            for car in net.cars:
                a = _get_assoc_info_fast(car)
                ssid = a['ssid'] or 'Not associated'
                bssid = a['bssid'] or 'N/A'
                ap_name = bssid_map.get(bssid, 'unknown-ap') if bssid != 'N/A' else 'N/A'
                signal = a['signal'] or 'N/A'
                info(f"  {car.name}: SSID={ssid}; BSSID={bssid}; AP={ap_name}; signal={signal}\n")

                if csv:
                    with open(csv, 'a') as f:
                        f.write(f"{ts},{car.name},{ssid},{bssid},{ap_name},{signal}\n")

                if a['bssid'] is None:
                    all_associated = False

            if all_associated:
                info(f"*** All {len(net.cars)} cars associated at {strftime('%H:%M:%S')}. Stopping logger.\n")
                stop_event.set()
                break

            if timeout is not None and (time.time() - start_time) > timeout:
                info(f"*** Association logger timed out after {timeout} seconds. Stopping.\n")
                stop_event.set()
                break

            time.sleep(interval)

    t = threading.Thread(target=logger, daemon=True)
    t.start()
    return stop_event
    #----------------------------------------------------------------------

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
        ap.setIP(ap_ips[i] + '/24', intf=ap.wintfs[0].name)
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

    print("*** Plotting Telemetry...")
    # Telemetry cars + APs
    nodes = net.cars + net.aps
    telemetry(nodes=nodes, data_type='position',
              min_x=-1200, min_y=-1500,
              max_x=4000, max_y=3000)

    # Wait for association, then start streaming
    assoc_csv = os.path.join(SHARED_DIR, 'assoc_log.csv')
    stop_event = start_assoc_logger_fast(net, interval=0.6, timeout=45, csv=assoc_csv)
    stop_event.wait()
    print("*** All cars associated.")
    
    print("*** Starting CLI (type exit to quit)...")
    CLI(net)

    print("*** Stopping network...")
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    topology()
