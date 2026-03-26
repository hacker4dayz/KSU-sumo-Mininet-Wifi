#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp, ipv6


class QoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

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

        if udp_pkt and ip_pkt.dst == "239.0.0.1" and udp_pkt.dst_port == 1234:
            self.logger.warning(
                "Video flow detected src=%s dst=%s sport=%s dport=%s in_port=%s",
                ip_pkt.src, ip_pkt.dst, udp_pkt.src_port, udp_pkt.dst_port, in_port
            )

            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=0x0800,
                ip_proto=17,
                ipv4_dst="239.0.0.1",
                udp_dst=1234
            )

            self.add_flow(datapath, 200, match, actions, msg.buffer_id)


        elif udp_pkt:
            self.logger.warning(
                "Best effort UDP src=%s dst=%s sport=%s dport=%s in_port=%s",
                ip_pkt.src, ip_pkt.dst, udp_pkt.src_port, udp_pkt.dst_port, in_port
            )

            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=0x0800,
                ip_proto=17,
                ipv4_src=ip_pkt.src,
                ipv4_dst=ip_pkt.dst,
                udp_src=udp_pkt.src_port,
                udp_dst=udp_pkt.dst_port
            )

            self.add_flow(datapath, 10, match, actions, msg.buffer_id)

        else:
            self.logger.warning(
                "Best effort IPv4 src=%s dst=%s proto=%s in_port=%s",
                ip_pkt.src, ip_pkt.dst, ip_pkt.proto, in_port
            )

            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=0x0800,
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