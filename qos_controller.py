#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp, ipv6
from ryu.lib.packet import ether_types

class QoSController(app_manager.RyuApp):
    # Declare that this app uses OpenFlow 1.3
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(QoSController, self).__init__(*args, **kwargs)

        # List of IP addresses for high‑priority emergency vehicles
        # These flows will get QoS priority 300
        self.high_priority_ips = [
            "10.0.0.213",  # Police IP (unicast stream receiver)
            "10.0.0.214"   # Ambulance IP (unicast stream receiver)
        ]

        # List of IP addresses for normal cars
        # These flows will get QoS priority 100
        self.normal_car_ips = [
        "10.0.0.201", "10.0.0.202", "10.0.0.203", "10.0.0.204", "10.0.0.205",
        "10.0.0.206", "10.0.0.207", "10.0.0.208", "10.0.0.209", "10.0.0.210", 
        "10.0.0.211", "10.0.0.212"
        ]
        

    # Helper function to install flow entries into switches
    # datapath: the switch’s datapath object
    # priority: flow entry’s priority (higher = more specific / higher QoS)
    # match: OFPMatch object describing packet fields to match
    # actions: list of actions to apply (e.g., output to port)
    # buffer_id: if the packet is buffered in switch, use it; else none
    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Build an instruction to apply the given actions
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        # If the packet is in switch buffer, include buffer_id
        if buffer_id is not None and buffer_id != ofproto.OFP_NO_BUFFER:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                priority=priority,
                match=match,
                instructions=inst
            )
        else:
            # No buffer: install flow without referencing a specific packet
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=inst
            )

        datapath.send_msg(mod)

    # Event handler for the switch‑features message (CONFIG_DISPATCHER stage)
    # Runs when a new switch connects and tells the controller its features
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        self.logger.info("SWITCH CONNECTED, datapath_id=%s", datapath.id)

        # Install a “table‑miss” flow entry: for unmatched packets,
        # send them to the controller (no buffering)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,ofproto.OFPCML_NO_BUFFER)]
        # Priority 0 (lowest): use this only when no other flow matches
        self.add_flow(datapath, 0, match, actions)

    # Event handler for Packet‑In messages (MAIN_DISPATCHER stage)
    # This is where the controller installs per‑flow QoS rules
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg # The PacketIn message
        datapath = msg.datapath # Switch that sent the packet
        ofproto = datapath.ofproto # OF1.3 constants
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"] # Input port on the switch

        # Parse the packet data into layers
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # If there is no Ethernet header, drop / ignore this packet
        if eth is None:
            return

        # Ignore LLDP packets (link‑layer discovery, not data traffic)
        if eth.ethertype == 0x88cc:
            return

        # Ignore IPv6 packets (only IPv4 traffic is handled)
        ipv6_pkt = pkt.get_protocol(ipv6.ipv6)
        if ipv6_pkt:
            return

        # Extract IPv4 and UDP headers (for traffic classification)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)

        # If this is not an IPv4 packet, flood it (no IP QoS)
        if not ip_pkt:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                # If not buffered, send the raw packet data
                data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            )
            datapath.send_msg(out)
            return

        # Default action: flood (no explicit flow installed yet)
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        priority = 5  # Default low‑priority for best‑effort traffic

        # ==================== EMERGENCY VEHICLE FLOWS (prio 300) ====================
        # If this is an IP packet involving a high‑priority IP (police or ambulance)
        if ip_pkt and (ip_pkt.dst in self.high_priority_ips or ip_pkt.src in self.high_priority_ips):
            priority = 300
            self.logger.warning("EMERGENCY %s -> %s prio=300", ip_pkt.src, ip_pkt.dst)

            # Match IPv4 flow (src + dst) on this switch
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
            )
            # If it is UDP, refine match to exact ports
            if udp_pkt:
                match.set_udp_src(udp_pkt.src_port)
                match.set_udp_dst(udp_pkt.dst_port)

            # Install flow entry with high priority 300
            self.add_flow(datapath, priority, match, actions, msg.buffer_id)

        # ==================== HIGH‑PRIORITY VIDEO MULTICAST FLOW (prio 200) ========
        # Match multicast video stream (IP 239.0.0.1, UDP port 1234)
        elif udp_pkt and ip_pkt.dst == "239.0.0.1" and udp_pkt.dst_port == 1234:
            priority = 200
            self.logger.warning("VIDEO %s -> 239.0.0.1:1234 prio=200", ip_pkt.src)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=17, # UDP protocol number
                ipv4_dst="239.0.0.1", # Multicast group
                udp_dst=1234 # Video stream port
            )

            self.add_flow(datapath, 200, match, actions, msg.buffer_id)

        # ==================== NORMAL CAR FLOWS (prio 100) ==========================
        # If the source IP is one of the normal cars, mark as high but not emergency
        elif ip_pkt and ip_pkt.src in self.normal_car_ips:
            priority = 100
            self.logger.warning("NORMAL %s -> %s prio=100", ip_pkt.src, ip_pkt.dst)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
            )
            if udp_pkt:
                match.set_udp_src(udp_pkt.src_port)
                match.set_udp_dst(udp_pkt.dst_port)

            self.add_flow(datapath, priority, match, actions, msg.buffer_id)
  
        # ==================== BEST‑EFFORT TRAFFIC (fallback, prio 5) ===============
        else:
            # Anything else (non‑emergency, non‑video, non‑normal‑car) is best‑effort
            self.logger.warning("BEST EFFORT %s -> %s prio=5", ip_pkt.src, ip_pkt.dst)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
                ip_proto=ip_pkt.proto
            )

            self.add_flow(datapath, 5, match, actions, msg.buffer_id)

        # ==================== SEND PACKET OUT (flooding for now) ===================
        # Send the current packet out via PacketOut (flooding) so it isn’t dropped
        # Only if the packet is not buffered in switch, we send raw data
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        )
        datapath.send_msg(out)
