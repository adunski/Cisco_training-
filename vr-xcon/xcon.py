#!/usr/bin/env python3

import fcntl
import logging
import os
import select
import signal
import socket
import struct
import sys


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)

def handle_SIGTERM(signal, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)


class Tcp2Tap:
    def __init__(self, tap_intf = 'tap0', listen_port=10001):
        self.logger = logging.getLogger()
        # setup TCP side
        self.s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.s.bind(('::0', 10001))
        self.s.listen(1)
        self.tcp = None

        # track current state of TCP side tunnel. 0 = reading size, 1 = reading packet
        self.tcp_state = 0
        self.tcp_buf = b''
        self.tcp_remaining = 0

        # setup tap side
        TUNSETIFF = 0x400454ca
        IFF_TUN = 0x0001
        IFF_TAP = 0x0002
        IFF_NO_PI = 0x1000
        self.tap = os.open("/dev/net/tun", os.O_RDWR)
        # we want a tap interface, no packet info and it should be called tap0
        # TODO: implement dynamic name using tap%d, right now we assume we are
        # only program in this namespace (docker container) that creates tap0
        ifs = fcntl.ioctl(self.tap, TUNSETIFF, struct.pack("16sH", tap_intf.encode(), IFF_TAP | IFF_NO_PI))
        # ifname - good for when we do dynamic interface name
        ifname = ifs[:16].decode().strip("\x00")


    def work(self):
        while True:
            skts = [self.s, self.tap]
            if self.tcp is not None:
                skts.append(self.tcp)
            ir = select.select(skts,[],[])[0][0]
            if ir == self.s:
                self.logger.debug("received incoming TCP connection, setting up!")
                self.tcp, addr = self.s.accept()
            elif ir == self.tcp:
                self.logger.debug("received packet from TCP and sending to tap interface")

                try:
                    buf = ir.recv(2048)
                except (ConnectionResetError, OSError):
                    self.logger.warning("connection dropped")
                    continue

                self.tcp_buf += buf
                self.logger.debug("read %d bytes from tcp, tcp_buf length %d" % (len(buf), len(self.tcp_buf)))
                while True:
                    if self.tcp_state == 0:
                        # we want to read the size, which is 4 bytes, if we
                        # don't have enough bytes wait for the next spin
                        if not len(self.tcp_buf) > 4:
                            self.logger.debug("reading size - less than 4 bytes available in buf; waiting for next spin")
                            break
                        size = socket.ntohl(struct.unpack("I", self.tcp_buf[:4])[0]) # first 4 bytes is size of packet
                        self.tcp_buf = self.tcp_buf[4:] # remove first 4 bytes of buf
                        self.tcp_remaining = size
                        self.tcp_state = 1
                        self.logger.debug("reading size - pkt size: %d" % self.tcp_remaining)

                    if self.tcp_state == 1: # read packet data
                        # we want to read the whole packet, which is specified
                        # by tcp_remaining, if we don't have enough bytes we
                        # wait for the next spin
                        if len(self.tcp_buf) < self.tcp_remaining:
                            self.logger.debug("reading packet - less than remaining bytes; waiting for next spin")
                            break
                        self.logger.debug("reading packet - reading %d bytes" % self.tcp_remaining)
                        payload = self.tcp_buf[:self.tcp_remaining]
                        self.tcp_buf = self.tcp_buf[self.tcp_remaining:]
                        self.tcp_remaining = 0
                        self.tcp_state = 0
                        os.write(self.tap, payload)

            else:
                # we always get full packets from the tap interface
                payload = os.read(self.tap, 2048)
                buf = struct.pack("I", socket.htonl(len(payload))) + payload
                if self.tcp is None:
                    self.logger.warning("received packet from tap interface but TCP not connected, discarding packet")
                else:
                    self.logger.debug("received packet from tap interface and sending to TCP")
                    self.tcp.send(buf)



class TcpBridge:
    def __init__(self):
        self.logger = logging.getLogger()
        self.sockets = []
        self.socket2remote = {}
        self.socket2hostintf = {}


    def hostintf2addr(self, hostintf):
        hostname, interface = hostintf.split("/")

        try:
            res = socket.getaddrinfo(hostname, "100%02d" % int(interface))
        except socket.gaierror:
            raise NoVR("Unable to resolve %s" % hostname)
        sockaddr = res[0][4]
        return sockaddr


    def add_p2p(self, p2p):
        source, destination = p2p.split("--")
        src_router, src_interface = source.split("/")
        dst_router, dst_interface = destination.split("/")

        src = self.hostintf2addr(source)
        dst = self.hostintf2addr(destination)

        left = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        right = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # dict to map back to hostname & interface
        self.socket2hostintf[left] = "%s/%s" % (src_router, src_interface)
        self.socket2hostintf[right] = "%s/%s" % (dst_router, dst_interface)

        try:
            left.connect(src)
        except:
            self.logger.info("Unable to connect to %s" % self.socket2hostintf[left])
        try:
            right.connect(dst)
        except:
            self.logger.info("Unable to connect to %s" % self.socket2hostintf[right])

        # add to list of sockets
        self.sockets.append(left)
        self.sockets.append(right)

        # dict for looking up remote in pair
        self.socket2remote[left] = right
        self.socket2remote[right] = left

        

    def work(self):
        while True:
            try:
                ir,_,_ = select.select(self.sockets, [], [])
            except select.error as exc:
                break

            for i in ir:
                remote = self.socket2remote[i]
                try:
                    buf = i.recv(2048)
                except ConnectionResetError as exc:
                    self.logger.warning("connection dropped, reconnecting to source %s" % self.socket2hostintf[i])
                    try:
                        i.connect(self.hostintf2addr(self.socket2hostintf[i]))
                        self.logger.debug("reconnect to %s successful" % self.socket2hostintf[i])
                    except Exception as exc:
                        self.logger.warning("reconnect failed %s" % str(exc))
                    continue
                except OSError as exc:
                    self.logger.warning("endpoint not connected, connecting to source %s" % self.socket2hostintf[i])
                    try:
                        i.connect(self.hostintf2addr(self.socket2hostintf[i]))
                        self.logger.debug("connect to %s successful" % self.socket2hostintf[i])
                    except:
                        self.logger.warning("connect failed %s" % str(exc))
                    continue

                if len(buf) == 0:
                    return
                self.logger.debug("%05d bytes %s -> %s " % (len(buf), self.socket2hostintf[i], self.socket2hostintf[remote]))
                try:
                    remote.send(buf)
                except BrokenPipeError:
                    self.logger.warning("unable to send packet %05d bytes %s -> %s due to remote being down, trying reconnect" % (len(buf), self.socket2hostintf[i], self.socket2hostintf[remote]))
                    try:
                        remote.connect(self.hostintf2addr(self.socket2hostintf[remote]))
                        self.logger.debug("connect to %s successful" % self.socket2hostintf[remote])
                    except Exception as exc:
                        self.logger.warning("connect failed %s" % str(exc))
                    continue



class NoVR(Exception):
    """ No virtual router
    """
            

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--debug', action="store_true", default=False, help='enable debug')
    parser.add_argument('--p2p', nargs='+', help='point-to-point link between virtual routers')
    parser.add_argument('--tap-listen', help='tap to virtual router. Will listen on specified port for incoming connection; 1 for TCP/10001')
    parser.add_argument('--tap-if', default="tap0", help='name of tap interface (use with other --tap-* arguments)')
    args = parser.parse_args()

    # sanity
    if args.p2p and args.tap_listen:
        print("--p2p and --tap-listen are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if args.debug:
        logger.setLevel(logging.DEBUG)

    if args.p2p:
        tt = TcpBridge()
        for p2p in args.p2p:
            try:
                tt.add_p2p(p2p)
            except NoVR as exc:
                print(exc, " Is it started and did you link it?")
                sys.exit(1)
        tt.work()

    if args.tap_listen:
        t2t = Tcp2Tap(args.tap_if, 10000 + int(args.tap_listen))
        t2t.work()
