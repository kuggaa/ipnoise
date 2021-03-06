#!/usr/bin/env python

"""
Copyright (c) 2016 Miroslav Stampar (@stamparm)
See the file 'LICENSE' for copying permission
"""

import csv
import datetime
import optparse
import glob
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True

from core.common import addr_to_int
from core.common import make_mask
from core.common import check_sudo
from core.settings import config
from core.settings import CSV_HEADER
from core.settings import DATE_FORMAT
from core.settings import DEFAULT_LOG_PERMISSIONS
from core.settings import ETH_LENGTH
from core.settings import HOST_ADDRESSES
from core.settings import IPPROTO
from core.settings import IPPROTO_LUT
from core.settings import LOCAL_NETWORKS
from core.settings import LOG_DIRECTORY
from core.settings import NAME
from core.settings import read_config
from core.settings import SENSOR_CONFIG_FILE
from core.settings import SNAP_LEN
from core.settings import SYSTEM_LOG_DIRECTORY
from core.settings import VERSION

try:
    import pcapy
except ImportError:
    if subprocess.mswindows:
        exit("[!] please install Pcapy (e.g. 'https://breakingcode.wordpress.com/?s=pcapy') and WinPcap (e.g. 'http://www.winpcap.org/install/')")
    else:
        exit("[!] please install Pcapy (e.g. 'apt-get install python-pcapy')")

_auxiliary = {}
_cap = None
_datalink = None
_traffic = {}

LAST_FILENAME = None
LAST_WRITE = None

def _get_sys_whitelist():
    retval = set()
    for filename in glob.glob(os.path.join(SYSTEM_LOG_DIRECTORY, "auth.log*")):
        with open(filename, "rb") as f:
            for line in f:
                if "]: Accepted" in line:
                    match = re.search(r"from ([\d.]+) port", line)
                    if match:
                        retval.add(match.group(1))

    _ = "/etc/resolv.conf"
    if os.path.isfile(_):
        with open(_, "rb") as f:
            for line in f:
                match = re.search(r"nameserver\s+([\d.]+)", line)
                if match:
                    retval.add(match.group(1))

    return retval

def _log_write(force=False, filename=None):
    global LAST_FILENAME
    global LAST_WRITE

    current = time.time()
    filename = filename or os.path.join(LOG_DIRECTORY, "%s.csv" % datetime.datetime.utcnow().strftime(DATE_FORMAT))
    whitelist = _get_sys_whitelist()

    if LAST_WRITE is None:
        LAST_WRITE = current

    if LAST_FILENAME is None:
        LAST_FILENAME = filename

    if force or (current - LAST_WRITE) > config.WRITE_PERIOD:
        if not os.path.exists(filename):
            open(filename, "w+").close()
            os.chmod(filename, DEFAULT_LOG_PERMISSIONS)

        with open(filename, "w+b") as f:
            results = []
            f.write("%s\n" % CSV_HEADER)

            for dst_key in _traffic:
                proto, dst_ip, dst_port = dst_key.split(":")
                for src_ip in _traffic[dst_key]:
                    if src_ip in whitelist:
                        continue
                    stat_key = "%s:%s" % (dst_key, src_ip)
                    first_seen, last_seen, count = _auxiliary[stat_key]
                    results.append((proto, src_ip, dst_ip, dst_port, first_seen, last_seen, count))

            for entry in sorted(results):
                f.write("%s\n" % " ".join(str(_) for _ in entry))

        LAST_WRITE = current

    if LAST_FILENAME != filename:
        if not force and LAST_WRITE != current:
            _log_write(True, LAST_FILENAME)

        LAST_FILENAME = filename

        _traffic.clear()
        _auxiliary.clear()

def _process_packet(packet, sec, usec):
    try:
        if _datalink == pcapy.DLT_LINUX_SLL:
            packet = packet[2:]

        eth_header = struct.unpack("!HH8sH", packet[:ETH_LENGTH])
        eth_protocol = socket.ntohs(eth_header[3])

        if eth_protocol == IPPROTO:  # IP
            ip_header = struct.unpack("!BBHHHBBH4s4s", packet[ETH_LENGTH:ETH_LENGTH + 20])
            ip_length = ip_header[2]
            packet = packet[:ETH_LENGTH + ip_length]  # truncate
            iph_length = (ip_header[0] & 0xF) << 2

            protocol = ip_header[6]
            src_ip = socket.inet_ntoa(ip_header[8])
            dst_ip = socket.inet_ntoa(ip_header[9])

            proto = IPPROTO_LUT.get(protocol)

            local_src = False
            for prefix, mask in LOCAL_NETWORKS:
                if addr_to_int(src_ip) & mask == prefix:
                    local_src = True
                    break

            if proto is None or any(_ in (config.IGNORE_ADDRESSES or "") for _ in (src_ip, dst_ip)):
                return

            if HOST_ADDRESSES and dst_ip not in HOST_ADDRESSES:
                return

            # only process SYN packets
            if protocol == socket.IPPROTO_TCP:      # TCP
                if local_src:
                    return

                i = iph_length + ETH_LENGTH
                src_port, dst_port, _, _, _, flags = struct.unpack("!HHLLBB", packet[i:i + 14])

                if any(str(_) in (config.IGNORE_PORTS or "") for _ in (src_port, dst_port)):
                    return

                dst_key = "%s:%s:%s" % (proto, dst_ip, dst_port)
                stat_key = "%s:%s" % (dst_key, src_ip)

                if flags == 2:                      # SYN set (only)
                    if dst_key not in _traffic:
                        _traffic[dst_key] = set()

                    _traffic[dst_key].add(src_ip)

                    if stat_key not in _auxiliary:
                        _auxiliary[stat_key] = [sec, sec, 1]
                    else:
                        _auxiliary[stat_key][1] = sec
                        _auxiliary[stat_key][2] += 1

            else:
                if protocol == socket.IPPROTO_UDP:  # UDP
                    i = iph_length + ETH_LENGTH
                    _ = packet[i:i + 4]
                    if len(_) < 4:
                        return

                    src_port, dst_port = struct.unpack("!HH", _)
                    if src_port < 1024 and dst_port > 1024 and dst_ip in HOST_ADDRESSES:        # potential reflection/amplification (junk) responses (e.g. service response to fake src_ip)
                        return
                else:                               # non-TCP/UDP (e.g. ICMP)
                    src_port, dst_port = '-', '-'

                if any(str(_) in (config.IGNORE_PORTS or "") for _ in (src_port, dst_port)):
                    return

                dst_key = "%s:%s:%s" % (proto, dst_ip, dst_port)
                stat_key = "%s:%s" % (dst_key, src_ip)

                flow = tuple(sorted((addr_to_int(src_ip), src_port, addr_to_int(dst_ip), dst_port)))

                if flow not in _auxiliary:
                    _auxiliary[flow] = True
 
                    if local_src:
                        return
 
                    if dst_key not in _traffic:
                        _traffic[dst_key] = set()

                    _traffic[dst_key].add(src_ip)
                    _auxiliary[stat_key] = [sec, sec, 1]

                elif stat_key in _auxiliary:
                    _auxiliary[stat_key][1] = sec
                    _auxiliary[stat_key][2] += 1

    except:
        if config.SHOW_DEBUG:
            traceback.print_exc()

    finally:
        _log_write()

def packet_handler(header, packet):
    try:
        sec, usec = header.getts()
        _process_packet(packet, sec, usec)
    except socket.timeout:
        pass

def init_sensor():
    global _cap
    global _datalink

    items = []

    for cmd, regex in (("/sbin/ifconfig", r"inet addr:([\d.]+) .*Mask:([\d.]+)"), ("ipconfig", r"IPv4 Address[^\n]+([\d.]+)\s+Subnet Mask[^\n]+([\d.]+)")):
        try:
            items = re.findall(regex, subprocess.check_output(cmd))
            break
        except OSError:
            pass

    for ip, mask in items:
        LOCAL_NETWORKS.append((addr_to_int(ip) & addr_to_int(mask), addr_to_int(mask)))
        HOST_ADDRESSES.add(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("google.com", 0))
        HOST_ADDRESSES.add(s.getsockname()[0])
    except:
        pass

    try:
        if not os.path.isdir(LOG_DIRECTORY):
            os.makedirs(LOG_DIRECTORY)
    except Exception, ex:
        if "Permission denied" in str(ex):
            exit("[x] please run with sudo/Administrator privileges")
        else:
            raise

    print "[i] using '%s' for log storage" % LOG_DIRECTORY

    print "[i] opening interface '%s'" % config.MONITOR_INTERFACE

    try:
        _cap = pcapy.open_live(config.MONITOR_INTERFACE, SNAP_LEN, True, 0)
    except socket.error, ex:
        if "permitted" in str(ex):
            exit("\n[x] please run with sudo/Administrator privileges")
        elif "No such device" in str(ex):
            exit("\n[x] no such device '%s'" % config.MONITOR_INTERFACE)
        else:
            raise
    except Exception, ex:
        if "Operation not permitted" in str(ex):
            exit("[x] please run with sudo/Administrator privileges")
        else:
            raise

    if config.CAPTURE_FILTER:
        print "[i] setting filter '%s'" % config.CAPTURE_FILTER
        _cap.setfilter(config.CAPTURE_FILTER)

    _datalink = _cap.datalink()
    if _datalink not in (pcapy.DLT_EN10MB, pcapy.DLT_LINUX_SLL):
        exit("[x] datalink type '%s' not supported" % _datalink)

    filename = os.path.join(LOG_DIRECTORY, "%s.csv" % datetime.datetime.utcnow().strftime(DATE_FORMAT))
    if os.path.exists(filename):
        with open(filename, "rb") as f:
            reader = csv.DictReader(f, delimiter=' ')
            for row in reader:
                dst_key = "%s:%s:%s" % (row["proto"], row["dst_ip"], row["dst_port"])
                stat_key = "%s:%s" % (dst_key, row["src_ip"])

                if dst_key not in _traffic:
                    _traffic[dst_key] = set()

                _traffic[dst_key].add(row["src_ip"])
                _auxiliary[stat_key] = [int(row["first_seen"]), int(row["last_seen"]), int(row["count"])]

def start_sensor():
    try:
        _cap.loop(-1, packet_handler)
    except SystemError:
        pass
    finally:
        _log_write(True)

def main():
    """
    Main function
    """

    print "%s (sensor) #v%s\n" % (NAME, VERSION)

    parser = optparse.OptionParser(version=VERSION)
    parser.add_option("-c", dest="config_file", default=SENSOR_CONFIG_FILE, help="Configuration file (default: '%s')" % os.path.split(SENSOR_CONFIG_FILE)[-1])
    options, _ = parser.parse_args()

    if not check_sudo():
        exit("[x] please run with sudo/Administrator privileges")

    read_config(options.config_file)
    init_sensor()

    try:
        start_sensor()
    except KeyboardInterrupt:
        print "\r[x] stopping (Ctrl-C pressed)"
    except:
        if config.SHOW_DEBUG:
            traceback.print_exc()
    finally:
        os._exit(0)

if __name__ == "__main__":
    main()
