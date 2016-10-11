#!/usr/bin/env python3

import datetime
import logging
import os
import random
import re
import signal
import sys
import telnetlib
import time

import IPy

def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)

def handle_SIGTERM(signal, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)
logging.Logger.trace = trace

def run_command(cmd, cwd=None, background=False):
    import subprocess
    res = None
    try:
        if background:
            p = subprocess.Popen(cmd, cwd=cwd)
        else:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, cwd=cwd)
            res = p.communicate()
    except:
        pass
    return res

def gen_mac(last_octet=None):
    """ Generate a random MAC address that is in the qemu OUI space and that
        has the given last octet.
    """
    return "52:54:00:%02x:%02x:%02x" % (
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff),
            last_octet
        )


class XRV:
    def __init__(self, username, password):
        self.logger = logging.getLogger()
        self.credentials = [
                ['admin', 'admin']
            ]
        self.spins = 0

        self.username = username
        self.password = password

        self.ram = 4096
        self.num_nics = 128

        self.xr_ready = False


    def start(self):
        """ Start the virtual router

            This can take a long time as we are waiting for the router to start
            and the do initial bootstraping of it over serial port.
        """
        start_time = datetime.datetime.now()
        self.start_vm()
        run_command(["socat", "TCP-LISTEN:22,fork", "TCP:127.0.0.1:2022"], background=True)
        run_command(["socat", "TCP-LISTEN:830,fork", "TCP:127.0.0.1:2830"], background=True)
        self.bootstrap_init()
        while True:
            done, res = self.bootstrap_spin()
            if done:
                break
        self.bootstrap_end()
        stop_time = datetime.datetime.now()
        self.logger.info("Startup complete in: %s" % (stop_time - start_time))



    def start_vm(self):
        """ Start the VM
        """
        self.logger.info("Starting VM")

        cmd = ["qemu-system-x86_64", "-display", "none", "-daemonize", "-m", str(self.ram),
               "-machine", "pc",
               "-device", "pci-bridge,chassis_nr=1,id=pci.0",
               "-device", "pci-bridge,chassis_nr=2,id=pci.1",
               "-device", "pci-bridge,chassis_nr=3,id=pci.2",
               "-device", "pci-bridge,chassis_nr=4,id=pci.3",
               "-device", "pci-bridge,chassis_nr=5,id=pci.4",
               "-device", "pci-bridge,chassis_nr=6,id=pci.5",
               "-serial", "telnet:0.0.0.0:5000,server,nowait",
               "-hda", "/xrv.vmdk"
               ]
        # enable hardware assist if KVM is available
        if os.path.exists("/dev/kvm"):
            cmd.insert(1, '-enable-kvm')

        # mgmt interface is special - we use qemu user mode network
        cmd.append("-device")
        cmd.append("e1000,netdev=mgmt,mac=%(mac)s"
                   % { 'mac': gen_mac(0) })
        cmd.append("-netdev")
        cmd.append("user,id=mgmt,net=10.0.0.0/24,hostfwd=tcp::2022-10.0.0.15:22,hostfwd=tcp::2830-10.0.0.15:830")

        for i in range(self.num_nics):
            import math
            nics_per_pci_bus = 26
            pci_bus = math.floor(i/nics_per_pci_bus)
            addr = i % nics_per_pci_bus
            cmd.append("-device")
            cmd.append("e1000,netdev=p%(i)02d,mac=%(mac)s,bus=pci.%(pci_bus)d,addr=0x%(addr)x"
                       % { 'i': i, 'mac': gen_mac(i), 'pci_bus': pci_bus+1, 'addr': addr+1 })
            cmd.append("-netdev")
            cmd.append("socket,id=p%(i)02d,listen=:100%(i)02d"
                       % { 'i': i+1 }) # offset by one if we want to mgmt on 0 later

        run_command(cmd)


    def bootstrap_init(self):
        """ Do the initial part of the bootstrap process
        """
        self.tn = telnetlib.Telnet("127.0.0.1", 5000)
        

    def bootstrap_spin(self):
        """ This function should be called periodically to do work.

            It can be used when you don't want to block waiting for the router
            to boot, like when you are booting multiple routers in parallel.

            returns True, True      when it's done and succeeded
            returns True, False     when it's done but failed
            returns False, False    when there is still work to be done
        """

        if self.spins > 300:
            # too many spins with no result ->  give up
            return True, False

        (ridx, match, res) = self.tn.expect([b"Press RETURN to get started",
            b"SYSTEM CONFIGURATION COMPLETE",
            b"Enter root-system username",
            b"Username:", b"^[^ ]+#"], 1)
        if match: # got a match!
            if ridx == 0: # press return to get started, so we press return!
                self.logger.debug("got 'press return to get started...'")
                self.wait_write("", wait=None)
            if ridx == 1: # system configuration complete
                self.logger.info("IOS XR system configuration is complete, should be able to proceed with bootstrap configuration")
                self.wait_write("", wait=None)
                self.xr_ready = True
            if ridx == 2: # initial user config
                self.logger.info("Creating initial user")
                self.wait_write(self.username, wait=None)
                self.wait_write(self.password, wait="Enter secret:")
                self.wait_write(self.password, wait="Enter secret again:")
                self.credentials.insert(0, [self.username, self.password])
            if ridx == 3: # matched login prompt, so should login
                self.logger.debug("matched login prompt")
                try:
                    username, password = self.credentials.pop(0)
                except IndexError as exc:
                    self.logger.error("no more credentials to try")
                    return True, False
                self.logger.debug("trying to log in with %s / %s" % (username, password))
                self.wait_write(username, wait=None)
                self.wait_write(password, wait="Password:")
            if self.xr_ready == True and ridx == 4:
                # run main config!
                self.bootstrap_config()
                return True, True

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b'':
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return False, False


    def bootstrap_config(self):
        """ Do the actual bootstrap config
        """
        self.logger.info("applying bootstrap configuration")
        self.wait_write("", None)
        self.wait_write("crypto key generate rsa\r")
        if self.username and self.password:
            self.wait_write("admin")
            self.wait_write("configure")
            self.wait_write("username %s group root-system" % (self.username))
            self.wait_write("username %s group cisco-support" % (self.username))
            self.wait_write("username %s secret %s" % (self.username, self.password))
            self.wait_write("commit")
            self.wait_write("exit")
            self.wait_write("exit")
        self.wait_write("configure")
        # configure netconf
        self.wait_write("ssh server v2")
        self.wait_write("ssh server netconf port 830") # for 5.1.1
        self.wait_write("ssh server netconf vrf default") # for 5.3.3
        self.wait_write("netconf agent ssh") # for 5.1.1
        self.wait_write("netconf-yang agent ssh") # for 5.3.3

        # configure xml agent
        self.wait_write("xml agent tty")

        # configure mgmt interface
        self.wait_write("interface MgmtEth 0/0/CPU0/0")
        self.wait_write("no shutdown")
        self.wait_write("ipv4 address 10.0.0.15/24")
        self.wait_write("exit")
        self.wait_write("commit")
        self.wait_write("exit")

    def bootstrap_end(self):
        self.tn.close()


    def wait_write(self, cmd, wait='#'):
        """ Wait for something and then send command
        """
        if wait:
            self.logger.trace("waiting for '%s' on serial console" % wait)
            res = self.tn.read_until(wait.encode())
            self.logger.trace("read from serial console: %s" % res.decode())
        self.logger.debug("writing to serial console: %s" % cmd)
        self.tn.write("{}\r".format(cmd).encode())



if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--trace', action='store_true', help='enable trace level logging')
    parser.add_argument('--username', default='vrnetlab', help='Username')
    parser.add_argument('--password', default='VR-netlab9', help='Password')
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = XRV(args.username, args.password)
    vr.start()
    logger.info("Going into sleep mode")
    while True:
        time.sleep(1)
