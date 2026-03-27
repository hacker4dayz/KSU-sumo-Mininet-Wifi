#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp, ipv6
from ryu.lib.packet import ether_types

class QoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(QoSController, self).__init__(*args, **kwargs)

        # EMERGENCY VEHICLES (prio 300)
        self.high_priority_ips = [
            "10.0.0.213",  # Police IP
            "10.0.0.214"   # Ambulance IP
        ]

        # BEST EFFORT NORMAL CARS (prio 100)
        self.normal_car_ips = [
        "10.0.0.201", "10.0.0.202", "10.0.0.203", "10.0.0.204", "10.0.0.205",
        "10.0.0.206", "10.0.0.207", "10.0.0.208", "10.0.0.209", "10.0.0.210", 
        "10.0.0.211", "10.0.0.212"
        ]
        

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        if buffer_id is not None and buffer_id != ofproto.OFP_NO_BUFFER:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                priority=priority,
                match=match,
                instructions=inst
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=inst
            )

        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        self.logger.info("SWITCH CONNECTED, datapath_id=%s", datapath.id)

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        if eth.ethertype == 0x88cc:
            return

        ipv6_pkt = pkt.get_protocol(ipv6.ipv6)
        if ipv6_pkt:
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)

        if not ip_pkt:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            )
            datapath.send_msg(out)
            return

        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        priority = 5  

        # Emergency vehicle:
        if ip_pkt and (ip_pkt.dst in self.high_priority_ips or ip_pkt.src in self.high_priority_ips):
            priority = 300
            self.logger.warning("EMERGENCY %s -> %s prio=300", ip_pkt.src, ip_pkt.dst)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
            )
            if udp_pkt:
                match.set_udp_src(udp_pkt.src_port)
                match.set_udp_dst(udp_pkt.dst_port)

            self.add_flow(datapath, priority, match, actions, msg.buffer_id)

        # High‑priority video multicast flow
        elif udp_pkt and ip_pkt.dst == "239.0.0.1" and udp_pkt.dst_port == 1234:
            priority = 200
            self.logger.warning("VIDEO %s -> 239.0.0.1:1234 prio=200", ip_pkt.src)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=17,
                ipv4_dst="239.0.0.1",
                udp_dst=1234
            )

            self.add_flow(datapath, 200, match, actions, msg.buffer_id)

        # Best‑effort UDP
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
  
        else:
            self.logger.warning("BEST EFFORT %s -> %s prio=5", ip_pkt.src, ip_pkt.dst)

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
                ip_proto=ip_pkt.proto
            )

            self.add_flow(datapath, 5, match, actions, msg.buffer_id)

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        )
        datapath.send_msg(out)

