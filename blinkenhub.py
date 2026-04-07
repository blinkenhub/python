
#
# Copyright (c) 2026 Systemix Software, Inc. All rights reserved.
#
# PROPRIETARY AND CONFIDENTIAL
#
# This software and its documentation are proprietary to Systemix Software, Inc.
# and may not be reproduced, distributed, disclosed, or used in any form
# without the express written permission of Systemix Software, Inc.
#
# Unauthorized copying, modification, distribution, or use of this software,
# via any medium, is strictly prohibited.
#
# For licensing inquiries, contact: info@systemix.com
#

import crc
import struct
from crc import Calculator, Crc8
import serial
import serial.tools.list_ports
from random import random
from time import sleep
import asyncio
from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.transport.serial import SMPSerialTransport
import ctypes
import subprocess


class TimeoutException(Exception):
	pass


class HSV_Color:

	def __init__(self, hue, saturation, value):
		self.h = hue
		self.s = saturation
		self.v = value

	def __bytes__(self):
		# encode in format suitable to transmission over the wire
		return struct.pack("<fff", self.h, self.s, self.v)


class Blinkenhub:

	ESC = 0x1b
	ACK = 'A'
	NACK = 'N'
	ETX = 0x03

	def __init__(self, port=None):
		self.calculator = Calculator(Crc8.CCITT)
		if port is None:
			# find blinkenhub
			for p in serial.tools.list_ports.comports():
				if p.manufacturer == 'Systemix' and p.product == 'Blinkenhub':
					port = p.device
					break
			if port is None:
				raise Exception("Blinkenhub not found")	
			
		self.ser = serial.Serial(port, timeout=.5)

	def prepare_buf(self, buf):
		# add crc
		raw = bytearray()
		raw += buf
		raw.append(self.calculator.checksum(raw))

		# escape special chars
		escbuf = bytearray()
		for b in raw:
			if b == self.ESC or b == self.ETX:
				escbuf.append(self.ESC)
			escbuf.append(b)

		# add the record terminator
		escbuf.append(self.ETX)

		return escbuf

	def send(self, item, reply=True):
		self.ser.write(self.prepare_buf(item.__bytes__()))
		if reply:
			resp = self.ser.read()
			if len(resp) < 1:
				raise TimeoutException()
			elif resp == b'A':
				return True
			else:
				return False
		else:
			return True


class BlinkenhubCmd:

	VERSION = 1

	CMD_SET_PIXELS          = 0
	CMD_ANIMATE_HUE         = 1
	CMD_ANIMATE_SATURATION  = 2
	CMD_ANIMATE_VALUE       = 3
	CMD_ROTATE              = 4
	CMD_SET_GAMMA			= 5

	def __init__(self, cmd):
		self.cmd = cmd

	def __bytes__(self):
		b = bytearray()
		b += struct.pack("<BBxx", self.cmd, self.VERSION)
		b += self.buf
		return b


class Animation(BlinkenhubCmd):

	ANIMATE_NONE = 0
	ANIMATE_UP = 1
	ANIMATE_DOWN = 2
	ANIMATE_UPDOWN = 3

	def __init__(self, cmd, typ, rate, top, bottom, count):
		BlinkenhubCmd.__init__(self, cmd)
		self.buf = struct.pack("<Bxxxfffi", typ, rate, top, bottom, count)


class SetGamma(BlinkenhubCmd):
	def __init__(self, gamma):
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_SET_GAMMA)
		self.buf = struct.pack("<f", gamma)


class SetPixels(BlinkenhubCmd):
	LEN_LIMIT = 17
	def __init__(self, colors, fade_time):
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_SET_PIXELS)
		if len(colors) > self.LEN_LIMIT:
			raise Exception("color vector too long")
		b = bytearray()
		for c in colors:
			b += c.__bytes__()
		fmt = f'<Bxxxf{len(b)}s'
		self.buf = struct.pack(fmt, len(colors), fade_time, b)
	

class HueAnimation(Animation):

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_HUE, typ, rate, top, bottom, count)


class SaturationAnimation(Animation):

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_SATURATION, typ, rate, top, bottom, count)


class ValueAnimation(Animation):

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_VALUE, typ, rate, top, bottom, count)


class Rotate(BlinkenhubCmd):

	NONE = 0
	CW = 1
	CCW = 2
	CYLON = 3

	def __init__(self, typ, rate, limit):
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_ROTATE)
		self.buf = struct.pack("<Bxxxfi", typ, rate, limit)


def mkrainbow(value):
	colors = []
	h = 0
	for _ in range(17):
		colors += [HSV_Color(h, 1, value)]
		h += 21.17647
	return colors


def stop_animations():
	bh = Blinkenhub(device)
	bh.send(Rotate(0,0,0))
	bh.send(HueAnimation(Animation.ANIMATE_UP,100,1,0,1))
	bh.send(SaturationAnimation(Animation.ANIMATE_UP,100,1,0,1))
	bh.send(ValueAnimation(Animation.ANIMATE_UP,100,1,0,1))


def mk_random_colors(value):
	colors = []
	for _ in range(17):
		if random() > 0.5:
			colors += [HSV_Color(random() * 360, 1, value)]
		else:
			colors +=[HSV_Color(0, 0, 0)]
	return colors


# Inspired by Claude
def are_we_awake(cg):
	# Get list of active display IDs
	max_displays = 16
	display_ids = (ctypes.c_uint32 * max_displays)()
	display_count = ctypes.c_uint32(0)
	cg.CGGetActiveDisplayList(max_displays, display_ids, ctypes.byref(display_count))
	sleeping = True
	for i in range(display_count.value):
		did = display_ids[i]
		if not bool(cg.CGDisplayIsAsleep(did)):
			sleeping = False
	return not sleeping


## 
##  Commands for exercising Blinkenhub
##

if __name__ == '__main__':

	import click

	device = None

	@click.group()
	@click.option('--port', envvar='BLINKENHUB_PORT', help='Serial port to which Blinkenhub is connected')
	def cli(port):
		global device
		device = port

	@cli.command()
	def run():
		# Load CoreGraphics
		cg = ctypes.cdll.LoadLibrary(
			"/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
		)

		# Set return type for CGDisplayIsAsleep (returns boolean int)
		cg.CGDisplayIsAsleep.restype = ctypes.c_int
		cg.CGGetActiveDisplayList.restype = ctypes.c_int

		bh = Blinkenhub(device)

		while(True):
			if are_we_awake(cg):
				stop_animations()
				while are_we_awake(cg):
					colors = mk_random_colors(.25)
					bh.send(SetPixels(colors, 0))
					sleep(.25)
			else:
				stop_animations()
				colors = [
					HSV_Color(0, 1, .1),
					HSV_Color(0, 1, .5),
					HSV_Color(0, 1, .1),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0),
					HSV_Color(0, 1, 0)
				]
				bh.send(SetPixels(colors, 0))
				bh.send(Rotate(Rotate.CYLON, 10, 14))
				while not are_we_awake(cg):
					sleep(.5)


	@cli.command()
	def stop():
		stop_animations()


	@cli.command()
	@click.option('-v', '--value', default=.5, help='The value (brightness)')
	@click.option('-r', '--rate', default=0.0, help='Spin rate')
	def rainbow(value, rate):
		stop_animations()
		bh = Blinkenhub(device)
		bh.send(SetPixels(mkrainbow(value), 0))
		if (rate > 0):
			bh.send(Rotate(Rotate.CW, rate, -1))


	@cli.command()
	@click.option('-p', '--period', default=0.3, help='Time in seconds of each step')
	@click.option('-v', '--value', default=.5, help='The value (brightness)')
	def disco(period, value):
		stop_animations()
		bh = Blinkenhub(device)
		while(True):
			colors = mk_random_colors(value)
			bh.send(SetPixels(colors, 0))
			sleep(period)

	@cli.command()
	@click.argument('gamma', default=1.0)
	def set_gamma(gamma):
		if Blinkenhub(device).send(SetGamma(gamma)):
			print('OK') 
		else:
			print('Error')

	@cli.command()
	@click.option('-p', '--pixel', type=(float, float, float), multiple=True)
	@click.option('-f', '--fade', default=0.0, help='Fade time in seconds)')
	@click.option('-d', '--default', type=(float, float, float), help='HSV values for the unspecified pixels')
	def set_pixels(pixel, fade, default):
		colors = []
		for p in pixel:
			colors += [HSV_Color(p[0], p[1], p[2])]
		print(default)
		if default is not None:
			d = HSV_Color(default[0], default[1], default[2])
		else:
			pi = len(pixel) - 1
			d = HSV_Color(pixel[pi][0], pixel[pi][1], pixel[pi][2])
		for _ in range(17 - len(pixel)):
			colors += [d]
		if Blinkenhub(device).send(SetPixels(colors, fade)):
			print('OK') 
		else:
			print('Error')

	@cli.command()
	@click.option('-n', '--none', 'typ', flag_value=Animation.ANIMATE_NONE, default=True)
	@click.option('-u', '--up', 'typ', flag_value=Animation.ANIMATE_UP)
	@click.option('-d', '--down', 'typ', flag_value=Animation.ANIMATE_DOWN)
	@click.option('-o', '--oscillate', 'typ', flag_value=Animation.ANIMATE_UPDOWN)
	@click.option('-r', '--rate', default=1.0, help='Rate of change as cycles per second)')
	@click.option('-t', '--top', default=360.0, help='Terminal high value [0.0, 360.0]')
	@click.option('-b', '--bottom', default=0.0, help='Terminal low value [0.0, 360.0]')
	@click.option('-c', '--count', default=1, help='Number of times to perform animation; -1 == forever')
	def animate_hue(typ, rate, top, bottom, count):
		if Blinkenhub(device).send(HueAnimation(typ, rate, top, bottom, count)):
			print('OK') 
		else:
			print('Error')

	@cli.command()
	@click.option('-n', '--none', 'typ', flag_value=Animation.ANIMATE_NONE, default=True)
	@click.option('-u', '--up', 'typ', flag_value=Animation.ANIMATE_UP)
	@click.option('-d', '--down', 'typ', flag_value=Animation.ANIMATE_DOWN)
	@click.option('-o', '--oscillate', 'typ', flag_value=Animation.ANIMATE_UPDOWN)
	@click.option('-r', '--rate', default=1.0, help='Rate of change as cycles per second)')
	@click.option('-t', '--top', default=1.0, help='Terminal high value')
	@click.option('-b', '--bottom', default=0.0, help='Terminal low value')
	@click.option('-c', '--count', default=1, help='Number of times to perform animation; -1 == forever')
	def animate_saturation(typ, rate, top, bottom, count):
		if Blinkenhub(device).send(SaturationAnimation(typ, rate, top, bottom, count)):
			print('OK') 
		else:
			print('Error')
		
	@cli.command()
	@click.option('-n', '--none', 'typ', flag_value=Animation.ANIMATE_NONE, default=True)
	@click.option('-u', '--up', 'typ', flag_value=Animation.ANIMATE_UP)
	@click.option('-d', '--down', 'typ', flag_value=Animation.ANIMATE_DOWN)
	@click.option('-o', '--oscillate', 'typ', flag_value=Animation.ANIMATE_UPDOWN)
	@click.option('-r', '--rate', default=1.0, help='Rate of change as cycles per second)')
	@click.option('-t', '--top', default=1.0, help='Terminal high value')
	@click.option('-b', '--bottom', default=0.0, help='Terminal low value')
	@click.option('-c', '--count', default=1, help='Number of times to perform animation; -1 == forever')
	def animate_value(typ, rate, top, bottom, count):
		if Blinkenhub(device).send(ValueAnimation(typ, rate, top, bottom, count)):
			print('OK') 
		else:
			print('Error')

	@cli.command()
	@click.option('--none', 'typ', flag_value=Rotate.NONE, default=True)
	@click.option('--cw', 'typ', flag_value=Rotate.CW)
	@click.option('--ccw', 'typ', flag_value=Rotate.CCW)
	@click.option('--cylon', 'typ', flag_value=Rotate.CYLON)
	@click.option('-r', '--rate', default=1.0, help='Rate of change as pixels per second)')
	@click.option('-l', '--limit', default=1, help='Number of pixels to rotate in cylon mode')
	def rotate(typ, rate, limit):
		if Blinkenhub(device).send(Rotate(typ, rate, limit)):
			print('OK') 
		else:
			print('Error')

	cli()
