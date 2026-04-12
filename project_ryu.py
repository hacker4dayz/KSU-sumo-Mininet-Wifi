#!/usr/bin/env python3

# Below is all the libraries that are being used
from mininet.log import setLogLevel, info
from mn_wifi.cli import CLI
from mn_wifi.net import Mininet_wifi
from mn_wifi.sumo.runner import sumo
from mn_wifi.link import wmediumd, ITSLink
from mn_wifi.wmediumdConnector import interference
from mn_wifi.telemetry import telemetry
from mininet.node import RemoteController
import os
import threading
import time
from time import strftime
import subprocess

#-----------------------------------------------------------------------
# Configurations:

# Shared directory for output files and logs
SHARED_DIR = os.path.abspath('.')
ref_TS_FILE = 'highway_mountain.ts' # Reference TS file used as the "original" video for PSNR comparison
VIDEO_FILE = 'highway_mountain.mp4' # Input video file used for streaming (converted to TS by ffmpeg)

# Helper function to ensure a directory exists
def ensure_dir(d):
    os.makedirs(d, exist_ok=True)

# Check if the input video file exists and prepare it for streaming
def prepare_video():
    video_path = os.path.join(SHARED_DIR, VIDEO_FILE)
    if not os.path.exists(video_path):
        print(f"*** ERROR: {VIDEO_FILE} not found!")
        return False
    print(f"*** Video ready: {video_path}")
    return True
#-------------------------------------------------------------------------
# ASSOCIATION LOGGER CODE
# This section handles logging which cars are associated with which APs

# Get the MAC address of the wlan0 interface of an AP (used to build BSSID map)
def get_ap_wlan0_addr(ap):
    intf = ap.wintfs[0].name # Get the name of the first wireless interface
    out = ap.cmd(f"iw dev {intf} info") # Run iw to get device info
    # Parse output for the MAC address line
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('addr '):
            return line.split()[1].strip().lower()
    # Fallback: use ip link if iw doesn't show addr
    out2 = ap.cmd(f"ip link show {intf}")
    for line in out2.splitlines():
        if 'link/ether' in line:
            return line.split()[1].strip().lower()
    return None

# Build a map from AP BSSID (MAC) to AP name
def build_bssid_map(net):
    bmap = {}
    for ap in net.aps: # Iterate over all access points
        mac = get_ap_wlan0_addr(ap) # Get the AP's MAC
        if mac:
            bmap[mac] = ap.name # Map BSSID to AP name
    return bmap

# Get the current Wi‑Fi association information for a car
def _get_assoc_info_fast(car):
    intf = car.wintfs[0].name # Use wlan0 for Wi‑Fi association
    out = car.cmd(f"iw dev {intf} link") # Run iw to get link info
    # Initialize dict to store SSID, BSSID, signal and raw output
    info_dict = {'ssid': None, 'bssid': None, 'signal': None, 'raw': out}
    for line in out.splitlines():
        line = line.strip()
        # Extract SSID from the SSID line
        if line.startswith('SSID:'):
            info_dict['ssid'] = line.split('SSID:')[1].strip()
        # Extract BSSID from Connected to line
        if line.startswith('Connected to'):
            parts = line.split()
            if len(parts) >= 3:
                info_dict['bssid'] = parts[2].strip().lower()
        # Extract signal level
        if line.startswith('signal:'):
            info_dict['signal'] = line.split('signal:')[1].strip()
    return info_dict

# Start a background thread that periodically logs association status for all cars
# interval: how often to poll (seconds)
# timeout: max time to wait before giving up
# csv: optional CSV file path to log data
# rebuild_map_every: how often to refresh the BSSID map
def start_assoc_logger_fast(net, interval=0.8, timeout=60, csv=None, rebuild_map_every=8):
    if csv:
        # Ensure the directory for the CSV file exists
        ensure_dir(os.path.dirname(csv) if os.path.dirname(csv) else '.')

    stop_event = threading.Event() # Event to stop the logger thread
    bssid_map = build_bssid_map(net) # Initial BSSID to AP name map
    
    # Logger function that runs in a separate thread
    def logger():
        start_time = time.time() # Time when logging started
        iteration = 0 # Iteration counter
        # If CSV logging is enabled, write the header
        if csv:
            with open(csv, 'w') as f: 
                f.write('timestamp,car,ssid,bssid,ap,signal\n')

         # Main loop: poll every interval until stopped or timeout
        while not stop_event.is_set():
            iteration += 1
            # Periodically rebuild the BSSID map so AP names stay up to date
            if rebuild_map_every and iteration % rebuild_map_every == 0:
                bssid_map.clear() # Clear old mapping
                bssid_map.update(build_bssid_map(net)) # Rebuild it

            # Current timestamp string
            ts = strftime('%Y-%m-%d %H:%M:%S')
            info(f"*** Association snapshot {strftime('%H:%M:%S')}\n")

            all_associated = True # Assume all cars are associated
            # Check each car’s association status
            for car in net.cars:
                a = _get_assoc_info_fast(car)
                ssid = a['ssid'] or 'Not associated'
                bssid = a['bssid'] or 'N/A'
                # Map BSSID to AP name; if not found use 'unknown-ap'
                ap_name = bssid_map.get(bssid, 'unknown-ap') if bssid != 'N/A' else 'N/A'
                signal = a['signal'] or 'N/A'
                # Log association info to Mininet info output
                info(f"  {car.name}: SSID={ssid}; BSSID={bssid}; AP={ap_name}; signal={signal}\n")

                # If CSV logging is requested, write individual record
                if csv:
                    with open(csv, 'a') as f:
                        f.write(f"{ts},{car.name},{ssid},{bssid},{ap_name},{signal}\n")

                # If this car is not associated, mark all_associated = False
                if a['bssid'] is None:
                    all_associated = False

            # If all cars are associated, stop the logger
            if all_associated:
                info(f"*** All {len(net.cars)} cars associated at {strftime('%H:%M:%S')}. Stopping logger.\n")
                stop_event.set()
                break

            # If timeout is reached, stop the logger
            if timeout is not None and (time.time() - start_time) > timeout:
                info(f"*** Association logger timed out after {timeout} seconds. Stopping.\n")
                stop_event.set()
                break

            time.sleep(interval) # Wait before the next polling iteration

    # Start the logger in a separate daemon thread
    t = threading.Thread(target=logger, daemon=True)
    t.start()
    return stop_event # Return the stop event so the caller can wait on it
# ---------------------------------------------------------------------
# FFMPEG VIDEO STREAMING AND RECORDING CODE
# This section handles video streaming from the server and recording on vehicles

# Start three UDP‑based video streams from the server using ffmpeg:
# 1. Police stream (unicast, high bitrate)
# 2. Ambulance stream (unicast, high bitrate)
# 3. General multicast stream (lower bitrate) for normal cars
def start_video_stream(server):
    print("*** Starting video stream on server...")

    # Police video stream (unicast, high bitrate, aimed at 213)
    server.cmd(
        f"ffmpeg -re -i highway_mountain.ts "
        f"-c:v libx264 -preset medium -tune zerolatency "
        f"-profile:v baseline -level 3.0 "
        f"-g 25 -keyint_min 25 -sc_threshold 0 "
        f"-b:v 2200k -maxrate 2600k -bufsize 4000k "
        f"-c:a aac -ar 44100 -b:a 128k "
        f"-f mpegts udp://10.0.0.213:1235?localaddr=10.0.0.100\&pkt_size=1316\&buffer_size=1048576 "
        f"> unicast_police_ryu.log 2>&1"
    )

    # Ambulance video stream (unicast, high bitrate, aimed at 214)
    server.cmd(
        f"ffmpeg -re -i highway_mountain.ts "
        f"-c:v libx264 -preset medium -tune zerolatency "
        f"-profile:v baseline -level 3.0 "
        f"-g 25 -keyint_min 25 -sc_threshold 0 "
        f"-b:v 2200k -maxrate 2600k -bufsize 4000k "
        f"-c:a aac -ar 44100 -b:a 128k "
        f"-f mpegts udp://10.0.0.214:1236?localaddr=10.0.0.100\&pkt_size=1316\&buffer_size=1048576 "
        f"> unicast_ambulance_ryu.log 2>&1"
    )
    
     # Multicast stream for normal cars (lower bitrate, to multicast group 239.0.0.1 on port 1234)
    server.cmd(
        f"ffmpeg -re -i highway_mountain.ts "
        f"-c:v libx264 -preset medium -tune zerolatency "
        f"-profile:v baseline -level 3.0 "
        f"-g 25 -keyint_min 25 -sc_threshold 0 "
        f"-b:v 500k -maxrate 800k -bufsize 1200k "
        f"-c:a aac -ar 44100 -b:a 128k "
        f"-f mpegts udp://239.0.0.1:1234?localaddr=10.0.0.100\&pkt_size=1316\&buffer_size=1048576 "
        f"> multicast_normal_ryu.log 2>&1"
    )

# Start recording on all vehicles (police, ambulance and normal cars)
# Each car runs ffmpeg to listen on a specific UDP port (unicast or multicast) and record TS
def start_recording(net):
    print("*** Starting recording on vehicles...")

    # Step 1: join multicast on all normal cars first
    print("*** Pre-joining multicast group on normal cars...")
    for car in net.cars:
        if car.name not in ['police', 'ambulance']:
            iface = f"{car.name}-wlan0" # Interface name on the car
            car.cmd(f"ip maddr add 239.0.0.1 dev {iface}") # Add multicast membership
            print(f"*** {car.name}: multicast joined on {iface}")

    # Give multicast membership time to settle
    time.sleep(2)

    # Step 2: start recording
    for car in net.cars:
        ts_file = f"{car.name}_ryu.ts"
        recv_log = f"{car.name}_ryu_recv.log"
        iface = f"{car.name}-wlan0"

        # Extract the car’s IP address from the interface
        ip = car.cmd(
            f"ip -4 addr show dev {iface} | grep inet | awk '{{print $2}}' | cut -d/ -f1"
        ).strip()
        print(f"*** {car.name} IP: {ip}")

        # Police listens on UDP 1235 (unicast stream from server to 213)
        if car.name == 'police':
            cmd = (
                f"ffmpeg -y -fflags +genpts+discardcorrupt -flags low_delay "
                f"-i udp://{ip}:1235?localaddr={ip}\&fifo_size=1000000\&overrun_nonfatal=1\&pkt_size=1316\&buffer_size=1048576 "
                f"-c copy -f mpegts {ts_file} "
                f"> {recv_log} 2>&1 &"
            )

        # Ambulance listens on UDP 1236 (unicast stream from server to 214)
        elif car.name == 'ambulance':
            cmd = (
                f"ffmpeg -y -fflags +genpts+discardcorrupt -flags low_delay "
                f"-i udp://{ip}:1236?localaddr={ip}\&fifo_size=1000000\&overrun_nonfatal=1\&pkt_size=1316\&buffer_size=1048576 "
                f"-c copy -f mpegts {ts_file} "
                f"> {recv_log} 2>&1 &"
            )

        # Normal cars listen on multicast 239.0.0.1:1234
        else:
            cmd = (
                f"ffmpeg -y -fflags +genpts+discardcorrupt -flags low_delay "
                f"-i udp://239.0.0.1:1234?localaddr={ip}\&fifo_size=1000000\&overrun_nonfatal=1\&pkt_size=1316\&buffer_size=1048576 "
                f"-c copy -f mpegts {ts_file} "
                f"> {recv_log} 2>&1 &"
            )

        car.cmd(cmd) # Start ffmpeg in background on each vehicle

# Convert all captured TS files to MP4 without re‑encoding (copy streams)
def convert_to_mp4(net):
    print("*** Converting TS to MP4...")
    
    for car in net.cars:
        ts_file = f"{car.name}_ryu.ts"
        mp4_file = f"{car.name}_ryu.mp4"

        cmd = f"ffmpeg -y -i {ts_file} -c copy {mp4_file}"
        car.cmd(cmd)

# Run PSNR analysis comparing each car’s recorded TS against the reference TS
def run_psnr(net):
    print("*** Running PSNR...")
    for car in net.cars:
        ts_file = f"{car.name}_ryu.ts"
        log_file = f"psnr_{car.name}_ryu.log"

        # Command runs ffmpeg with lavfi psnr filter:
        # -i {ts_file}         : input/distorted video
        # -i {ref_TS_FILE}     : reference (original) video
        # -lavfi psnr=...      : compute PSNR and write stats to log_file
        cmd = (
            f"ffmpeg -i {ts_file} -i {ref_TS_FILE} "           # distorted, reference
            f"-lavfi \"[0:v]settb=AVTB,setpts=PTS-STARTPTS[dist];"  # [0] = distorted
            f"[1:v]settb=AVTB,setpts=PTS-STARTPTS[ref];"         # [1] = reference  
            f"[dist][ref]psnr=stats_file={log_file}\" "
            f"-f null -"
        )
        car.cmd(cmd)
#----------------------------------------------------------------------
# MAIN TOPOLOGY AND EXPERIMENT FLOW
def topology():

    # Make sure the input video exists before proceeding
    if not prepare_video():
        return

    # Ensure the shared directory exists
    ensure_dir(SHARED_DIR)

    # Create the Wi‑Fi Mininet‑WiFi network with a RemoteController and interference model
    net = Mininet_wifi(
        controller=RemoteController,
        link=wmediumd, # Use wmediumd for realistic wireless medium
        wmediumd_mode=interference # Use interference-based propagation
    )

    print("*** Starting...")
    print("*** Adding nodes...")

    # Add 12 normal cars
    cars = []
    for x in range(0, 12):
        car = net.addCar(f'car{x+1}', wlans=2) # Each car has 2 WLAN interfaces
        car.params['priority'] = 'normal' # Set QoS priority for traffic
        cars.append(car)

    # Add emergency vehicles: police and ambulance
    police = net.addCar('police', wlans=2)
    ambulance = net.addCar('ambulance', wlans=2)
    police.params['priority'] = 'high' # High QoS priority
    ambulance.params['priority'] = 'high' # High QoS priority
    print("*** Priority: police/high, ambulance/high, others/normal")

    # Common Wi‑Fi parameters for access points
    kwargs = {
        'ssid': 'roadside-ssid',
        'mode': 'g',
        'datapath': 'user'
    }
    ap1 = net.addAccessPoint('ap1', channel='1', position='900,830,0', **kwargs)
    ap2 = net.addAccessPoint('ap2', channel='6', position='2000,100,0', **kwargs)
    ap3 = net.addAccessPoint('ap3', channel='11', position='750,1400,0', **kwargs)
    ap4 = net.addAccessPoint('ap4', channel='1', position='705,265,0', **kwargs)
    ap5 = net.addAccessPoint('ap5', channel='6', position='1635,1265,0', **kwargs)

    # Add a server station that acts as the video streaming source
    # wlans=1: only one wireless interface
    # IP 10.0.0.100/24 on the backtrack/roadside network
    # Position is near ap1 (same general area)
    server = net.addStation('server', wlans=1, ip='10.0.0.100/24', position='902,832,0' )
    
    # Create a RemoteController instance that will be used by all APs
    # Runs on localhost (127.0.0.1) at the default OpenFlow port 6633
    # Uses TCP as the protocol
    c0 = RemoteController('c0', ip='127.0.0.1', port=6633, protocols='tcp')

    print("*** Configuring Propagation Model")
    # Set a log‑normal shadowing wireless propagation model with path‑loss exponent 2.7
    net.setPropagationModel(model="logNormalShadowing", exp=2.7)

    print("*** Configuring nodes")
    # Configure all Wi‑Fi nodes (APs and cars) so they are ready for simulation
    net.configureWifiNodes()

    # AP backbone links (tree)
    print("*** Creating AP backbone links...")
    net.addLink(ap1, c0)
    net.addLink(ap1, ap2)
    net.addLink(ap1, ap3)
    net.addLink(ap1, ap4)
    net.addLink(ap1, ap5)
    net.addLink(server, ap1)

    # ITSLink for DSRC (wlan1)
    print("*** Adding ITSLinks (DSRC channel 181)...")
    # Add ITS‑specific wireless links using the DSRC channel (802.11p context)
    # Each car uses wlan1 (second interface) for DSRC communication on channel 181 (5.9 GHz)
    for car in net.cars:
        net.addLink(car, intf=car.wintfs[1].name, cls=ITSLink, band=20, channel=181)

    # Ensure SUMO cfg is in the same dir
    # Check that the SUMO configuration file exists in the current directory
    if not os.path.exists('ksuroadtest.sumocfg'):
        print("*** WARNING: ksuroadtest.sumocfg not found in current directory.")
    
    # Start SUMO
    print("*** Starting SUMO simulation...")

    # Attach SUMO as an external mobility program to the Wi‑Fi network
    net.useExternalProgram(
        program=sumo,
        port=8813, # TCP port for SUMO–mininet communication
        config_file='ksuroadtest.sumocfg', # Road network config
        extra_params=["--start", "--delay", "1000"], # Start SUMO immediately with 1000 ms delay
        clients=1, # One client (SUMO) connecting to the mobility socket
        exec_order=0 # Execute this program first in the external‑program sequence
    )

    print("*** Building network...")
    net.build()

    print("*** Configuring AP wlan0 IPs...")
    # Assign static IPs to the APs’ wlan0 interfaces (roadside Wi‑Fi network)
    ap_ips = ['10.0.0.101', '10.0.0.102', '10.0.0.103', '10.0.0.104', '10.0.0.105']
    for i, ap in enumerate(net.aps):
        ap.setIP(ap_ips[i] + '/24', intf=ap.wintfs[0].name) # Set IP on wlan0
        print(f"  {ap.name}-wlan0: {ap_ips[i]}")

    # Configure IP addresses and default routes for all cars
    for i, car in enumerate(net.cars):
        ip = '10.0.0.%d/24' % (200 + i + 1) # Unique IP for each car
        intf = car.wintfs[0].name # First WLAN interface (wlan0)
        car.cmd(f'ip addr flush dev {intf}') # Remove any existing IPs
        car.cmd(f'ip addr add {ip} dev {intf}') # Assign new IP
        car.cmd('ip route add default via 10.0.0.101') # Route all traffic via ap1

    print("*** Starting controller and APs")
    c0.start()

    # Start each AP and configure its OpenFlow controller connection
    for ap in net.aps:
        print(f"*** Starting {ap.name}...")
        ap.start([c0])

        # Configure the AP’s OVS switch to connect to the controller via TCP
        ap.cmd(f'ovs-vsctl set-controller {ap.name} tcp:127.0.0.1:6633')
        # Set the OpenFlow protocol version to OpenFlow 1.3
        ap.cmd(f'ovs-vsctl set Bridge {ap.name} protocols=OpenFlow13')

    print("*** Waiting 5s for Ryu controller connection...")
    # Give the Ryu controller time to establish OpenFlow sessions with all APs
    time.sleep(5)

    print("*** Plotting Telemetry...")
    # telemetry() plots node positions (cars and APs) in real time
    nodes = net.cars + net.aps # Include both cars and APs in the telemetry
    telemetry(
        nodes=nodes,
        data_type='position', # Visualize node positions
        min_x=-1200, min_y=-1500, # Map bounding box (x, y min)
        max_x=4000, max_y=3000, # Map bounding box (x, y max)
    )

    # Wait for association, then start streaming
    # Log all car–AP associations to CSV while waiting
    assoc_csv = os.path.join(SHARED_DIR, 'assoc_log_ryu.csv')
    stop_event = start_assoc_logger_fast(net, interval=0.6, timeout=45, csv=assoc_csv)
    stop_event.wait()  # Wait until logger notifies that all cars associated
    print("*** All cars associated.")

    # Start Recording (Vehicles are listening)
    # Vehicles start ffmpeg to record incoming streams (before or while streaming)
    start_recording(net)
    
    # Start Streaming (Server is Streaming)
    # Server starts three ffmpeg streams (police, ambulance, multicast)
    start_video_stream(server)
    
    # Convert files
    # After recording, convert each recorded TS to MP4 for easier viewing
    convert_to_mp4(net)

    # Run PSNR
    # Compute PSNR between each car’s recorded TS and the original reference TS
    run_psnr(net)

    print("*** Video processing complete.")

    print("*** Starting CLI (type exit to quit)...")
    CLI(net)

    print("*** Stopping network...")
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    topology()
