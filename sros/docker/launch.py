#!/usr/bin/env python3

import datetime
import logging
import math
import os
import random
import re
import signal
import subprocess
import sys
import telnetlib
import time

import IPy

import vrnetlab

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


def mangle_uuid(uuid):
    """ Mangle the UUID to fix endianness mismatch on first part
    """
    parts = uuid.split("-")

    new_parts = [
        uuid_rev_part(parts[0]),
        uuid_rev_part(parts[1]),
        uuid_rev_part(parts[2]),
        parts[3],
        parts[4]
    ]

    return '-'.join(new_parts)


def uuid_rev_part(part):
    """ Reverse part of a UUID
    """
    res = ""
    for i in reversed(range(0, len(part), 2)):
        res += part[i]
        res += part[i+1]
    return res




class SROS_vm(vrnetlab.VM):
    def __init__(self, username, password, num=0, ram=6144):
        super(SROS_vm, self).__init__(username, password, disk_image = "/sros.qcow2", num=num, cpus=2, ram=ram)

        self.uuid = "00000000-0000-0000-0000-000000000000"
        self.read_license()




    def bootstrap_spin(self):
        """ This function should be called periodically to do work.
        """

        if self.spins > 60:
            # too many spins with no result, probably means SROS hasn't started
            # successfully, so we restart it
            self.logger.warning("no output from serial console, restarting VM")
            self.stop()
            self.start()
            self.spins = 0
            return

        (ridx, match, res) = self.tn.expect([b"Login:", b"^[^ ]+#"], 1)
        if match: # got a match!
            if ridx == 0: # matched login prompt, so should login
                self.logger.debug("matched login prompt")
                self.wait_write("admin", wait=None)
                self.wait_write("admin", wait="Password:")
            # run main config!
            self.bootstrap_config()
            # close telnet connection
            self.tn.close()
            # calc startup time
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info("Startup complete in: %s" % startup_time)
            self.running = True
            return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b'':
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return


    def read_license(self):
        """ Read the license file, if it exists, and extract the UUID and start
            time of the license
        """
        if not os.path.isfile("/tftpboot/license.txt"):
            self.logger.info("No license file found")
            return

        lic_file = open("/tftpboot/license.txt", "r")
        license = ""
        for line in lic_file.readlines():
            # ignore comments in license file
            if line.startswith('#'):
                continue
            license += line
        lic_file.close()
        try:
            uuid_input = license.split(" ")[0]
            self.uuid = mangle_uuid(uuid_input)
            self.uuid = uuid_input
            m = re.search("([0-9]{4}-[0-9]{2}-)([0-9]{2})", license)
            if m:
                self.fake_start_date = "%s%02d" % (m.group(1), int(m.group(2))+1)
        except:
            raise ValueError("Unable to parse license file")
        self.logger.info("License file found for UUID %s with start date %s" % (self.uuid, self.fake_start_date))


class SROS_integrated(SROS_vm):
    """ Integrated VSR-SIM
    """
    def __init__(self, username, password, newchassis=False):
        super(SROS_integrated, self).__init__(username, password)

        self.num_nics = 5
        self.newchassis = newchassis
        if self.newchassis:
            self.smbios = ["type=1,product=TIMOS:address=10.0.0.15/24@active license-file=tftp://10.0.0.2/license.txt slot=A chassis=SR-1 card=iom-1 mda/1=me6-100gb-qsfp28"]
        else:
            self.smbios = ["type=1,product=TIMOS:address=10.0.0.15/24@active license-file=tftp://10.0.0.2/license.txt slot=A chassis=SR-c12 card=cfm-xp-b mda/1=m20-1gb-xp-sfp"]



    def gen_mgmt(self):
        """ Generate mgmt interface(s)

            We override the default function since we want a fake NIC in there
        """
        # call parent function to generate first mgmt interface (e1000)
        res = super(SROS_integrated, self).gen_mgmt()
        # add virtio NIC for internal control plane interface to vFPC
        res.append("-device")
        res.append("e1000,netdev=dummy0,mac=%s" % vrnetlab.gen_mac(1))
        res.append("-netdev")
        res.append("tap,ifname=dummy0,id=dummy0,script=no,downscript=no")
        return res



    def bootstrap_config(self):
        """ Do the actual bootstrap config
        """
        if self.username and self.password:
            self.wait_write("configure system security user \"%s\" password %s" % (self.username, self.password))
            self.wait_write("configure system security user \"%s\" access console netconf" % (self.username))
            self.wait_write("configure system security user \"%s\" console member \"administrative\" \"default\"" % (self.username))
        self.wait_write("configure system netconf no shutdown")
        self.wait_write("configure system security profile \"administrative\" netconf base-op-authorization lock")
        self.wait_write("configure card 1 mda 1 shutdown")
        self.wait_write("configure card 1 mda 1 no mda-type")
        self.wait_write("configure card 1 shutdown")
        self.wait_write("configure card 1 no card-type")
        if self.newchassis:
            self.wait_write("configure card 1 card-type iom-1 level he")
            self.wait_write("configure card 1 mda 1 mda-type me6-100gb-qsfp28")
        else:
            self.wait_write("configure card 1 card-type iom-xp-b")
            self.wait_write("configure card 1 mcm 1 mcm-type mcm-xp")
            self.wait_write("configure card 1 mda 1 mda-type m20-1gb-xp-sfp")
        self.wait_write("configure card 1 no shutdown")
        self.wait_write("admin save")
        self.wait_write("logout")




class SROS_cp(SROS_vm):
    """ Control plane for distributed VSR-SIM
    """
    def __init__(self, username, password, num_lc=1, newchassis=False):
        super(SROS_cp, self).__init__(username, password)
        self.newchassis = newchassis
        self.num_lc = num_lc

        self.num_nics = 0
        if self.newchassis:
            self.smbios = ["type=1,product=TIMOS:address=10.0.0.15/24@active license-file=tftp://10.0.0.2/license.txt chassis=SR-14s slot=A sfm=sfm-s card=cpm-s"]
        else:
            self.smbios = ["type=1,product=TIMOS:address=10.0.0.15/24@active license-file=tftp://10.0.0.2/license.txt chassis=XRS-20 chassis-topology=XRS-40 slot=A sfm=sfm-x20-b card=cpm-x20"]


    def start(self):
        # use parent class start() function
        super(SROS_cp, self).start()
        # add interface to internal control plane bridge
        vrnetlab.run_command(["brctl", "addif", "int_cp", "vcp-int"])
        vrnetlab.run_command(["ip", "link", "set", "vcp-int", "up"])
        vrnetlab.run_command(["ip", "link", "set", "dev", "vcp-int", "mtu", "10000"])



    def gen_mgmt(self):
        """ Generate mgmt interface(s)

            We override the default function since we want a NIC to the vFPC
        """
        # call parent function to generate first mgmt interface (e1000)
        res = super(SROS_cp, self).gen_mgmt()
        # add virtio NIC for internal control plane interface to vFPC
        res.append("-device")
        res.append("e1000,netdev=vcp-int,mac=%s" % vrnetlab.gen_mac(1))
        res.append("-netdev")
        res.append("tap,ifname=vcp-int,id=vcp-int,script=no,downscript=no")
        return res



    def bootstrap_config(self):
        """ Do the actual bootstrap config
        """
        if self.username and self.password:
            self.wait_write("configure system security user \"%s\" password %s" % (self.username, self.password))
            self.wait_write("configure system security user \"%s\" access console netconf" % (self.username))
            self.wait_write("configure system security user \"%s\" console member \"administrative\" \"default\"" % (self.username))
        self.wait_write("configure system netconf no shutdown")
        self.wait_write("configure system security profile \"administrative\" netconf base-op-authorization lock")

        # configure Power on new chassis
        if self.newchassis:
            for i in range(1,3):
                self.wait_write("configure system power-shelf {} power-shelf-type ps-a10-shelf-dc".format(i))
                for m in range(1, 11):
                    self.wait_write("configure system power-shelf {} power-module {} power-module-type ps-a-dc-6000".format(i, m))

        # configure SFMs
        if self.newchassis:
            for i in range(1, 9):
                self.wait_write("configure sfm {} sfm-type sfm-s".format(i))
        else:
            for i in range(1, 17):
                self.wait_write("configure sfm {} sfm-type sfm-x20-b".format(i))

        # configure line card & MDAs
        for i in range(1, self.num_lc+1):
            if self.newchassis:
                pass
            else:
                self.wait_write("configure card {} card-type xcm-x20".format(i))
                self.wait_write("configure card {} mda 1 mda-type cx20-10g-sfp".format(i))

        self.wait_write("admin save")
        self.wait_write("logout")




class SROS_lc(SROS_vm):
    """ Line card for distributed VSR-SIM
    """
    def __init__(self, slot=1, newchassis=False):
        super(SROS_lc, self).__init__(None, None, num=slot)
        self.slot = slot
        self.newchassis = newchassis
        
        self.num_nics = 6
        if self.newchassis:
            self.smbios = ["type=1,product=TIMOS:chassis=SR-14s slot={} sfm=sfm-s card=xcm-14s mda/1=s36-400gb-qsfpdd".format(slot)]
        else:
            self.smbios = ["type=1,product=TIMOS:chassis=XRS-20 chassis-topology=XRS-40 slot={} sfm=sfm-x20-b card=xcm-x20 mda/1=cx20-10g-sfp".format(slot)]



    def start(self):
        # use parent class start() function
        super(SROS_lc, self).start()
        # add interface to internal control plane bridge
        vrnetlab.run_command(["brctl", "addif", "int_cp", "vfpc{}-int".format(self.slot)])
        vrnetlab.run_command(["ip", "link", "set", "vfpc{}-int".format(self.slot), "up"])
        vrnetlab.run_command(["ip", "link", "set", "dev", "vfpc{}-int".format(self.slot), "mtu", "10000"])



    def gen_mgmt(self):
        """ Generate mgmt interface
        """
        res = []
        # mgmt interface
        res.extend(["-device", "e1000,netdev=mgmt,mac=%s" % vrnetlab.gen_mac(0)])
        res.extend(["-netdev", "user,id=mgmt,net=10.0.0.0/24"])
        # internal control plane interface to vFPC
        res.extend(["-device", "e1000,netdev=vfpc-int,mac=%s" %
                    vrnetlab.gen_mac(0)])
        res.extend(["-netdev",
                    "tap,ifname=vfpc{}-int,id=vfpc-int,script=no,downscript=no".format(self.slot)])
        return res


    def gen_nics(self):
        """ Generate qemu args for the normal traffic carrying interface(s)
        """
        res = []
        # TODO: should this offset business be put in the common vrnetlab?
        offset = 6 * (self.slot-1)
        for j in range(0, self.num_nics):
            i = offset + j + 1
            res.append("-device")
            res.append(self.nic_type + ",netdev=p%(i)02d,mac=%(mac)s"
                       % { 'i': i, 'mac': vrnetlab.gen_mac(i) })
            res.append("-netdev")
            res.append("socket,id=p%(i)02d,listen=:100%(i)02d"
                       % { 'i': i })
        return res



    def bootstrap_spin(self):
        """ We have nothing to do for VSR-SIM line cards
        """
        self.running = True
        self.tn.close()
        return



class SROS(vrnetlab.VR):
    def __init__(self, username, password, num_nics, newchassis=False):
        super(SROS, self).__init__(username, password)

        # move files into place
        for e in os.listdir("/"):
            if re.search("\.qcow2$", e):
                os.rename("/" + e, "/sros.qcow2")
            if re.search("\.license$", e):
                os.rename("/" + e, "/tftpboot/license.txt")

        self.license = False
        if os.path.isfile("/tftpboot/license.txt"):
            self.logger.info("License found")
            self.license = True

        self.logger.info("Number of NICS: " + str(num_nics))
        # if we have more than 5 NICs we use distributed VSR-SIM
        if num_nics > 5:
            if not self.license:
                self.logger.error("More than 5 NICs require distributed VSR which requires a license but no license is found")
                sys.exit(1)

            num_lc = math.ceil(num_nics / 6)
            self.logger.info("Number of linecards: " + str(num_lc))
            self.vms = [ SROS_cp(username, password, num_lc=num_lc, newchassis=newchassis) ]
            for i in range(1, num_lc+1):
                self.vms.append(SROS_lc(i, newchassis=newchassis))

        else: # 5 ports or less means integrated VSR-SIM
            self.vms = [ SROS_integrated(username, password, newchassis=newchassis) ]

        # set up bridge for connecting CP with LCs
        vrnetlab.run_command(["brctl", "addbr", "int_cp"])
        vrnetlab.run_command(["ip", "link", "set", "int_cp", "up"])




if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--trace', action='store_true', help='enable trace level logging')
    parser.add_argument('--username', default='vrnetlab', help='Username')
    parser.add_argument('--password', default='VR-netlab9', help='Password')
    parser.add_argument('--num-nics', default=5, help='Number of NICs')
    parser.add_argument('--newchassis', help='Use New Chassis SR-1, SR-14s', action="store_true")
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    ia = SROS(args.username, args.password, num_nics=int(args.num_nics), newchassis=args.newchassis)
    ia.start()
