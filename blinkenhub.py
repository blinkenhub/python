
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

"""Control library and command-line interface for the Blinkenhub Mac mini stand.

Blinkenhub is a USB-attached ring of 17 RGB LEDs. This module talks to it over
a serial port using a simple framed protocol: each command is serialized to
little-endian binary and followed by a CRC-8 (CCITT) checksum. ``ESC`` and
``ETX`` bytes are escaped, and the frame ends with ``ETX``. The device answers
each frame with a single ``A`` (ACK) or ``N`` (NACK) byte.

Colors are expressed in HSV: hue in degrees ``[0.0, 360.0]``, and saturation
and value (brightness) in ``[0.0, 1.0]``.

Example:
	Show a dim rainbow and spin it clockwise::

		from blinkenhub import Blinkenhub, SetPixels, Rotate, mkrainbow

		bh = Blinkenhub()  # auto-detect the device
		bh.send(SetPixels(mkrainbow(0.25), 0))
		bh.send(Rotate(Rotate.CW, 2.0, -1))

The ``blinkenhub`` console script (see :func:`main`) exposes the same
functionality from the shell; run ``blinkenhub --help`` for details.
"""

import struct
from crc import Calculator, Crc8
import serial
import serial.tools.list_ports
from random import random
from time import sleep
from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.transport.serial import SMPSerialTransport
import ctypes


#: Serial port used by :func:`stop_animations` and the CLI commands. ``None``
#: means auto-detect. The CLI sets it from ``--port`` / ``BLINKENHUB_PORT``.
device = None


class TimeoutException(Exception):
	"""Raised when the Blinkenhub does not reply to a command before the serial timeout."""
	pass


class HSV_Color:
	"""A single pixel color in the HSV color space.

	Attributes:
		h (float): Hue in degrees, ``[0.0, 360.0]``.
		s (float): Saturation, ``[0.0, 1.0]``.
		v (float): Value (brightness), ``[0.0, 1.0]``.
	"""

	def __init__(self, hue, saturation, value):
		"""Create a color.

		Args:
			hue (float): Hue in degrees, ``[0.0, 360.0]``.
			saturation (float): Saturation, ``[0.0, 1.0]``.
			value (float): Value (brightness), ``[0.0, 1.0]``.
		"""
		self.h = hue
		self.s = saturation
		self.v = value

	def __bytes__(self):
		"""Encode the color for transmission to the device.

		Returns:
			bytes: Three little-endian 32-bit floats ``(h, s, v)``, 12 bytes total.
		"""
		# encode in format suitable to transmission over the wire
		return struct.pack("<fff", self.h, self.s, self.v)


class Blinkenhub:
	"""Serial connection to a Blinkenhub device.

	Handles device discovery, framing, and the ACK/NACK handshake. Commands are
	built from :class:`BlinkenhubCmd` subclasses and sent with :meth:`send`.

	Attributes:
		ESC (int): Escape byte; precedes any ``ESC`` or ``ETX`` byte in the payload.
		ACK (str): Reply sent by the device when it accepts a command.
		NACK (str): Reply sent by the device when it rejects a command.
		ETX (int): End-of-frame byte.
		calculator (crc.Calculator): CRC-8 (CCITT) calculator for frame checksums.
		ser (serial.Serial): The open serial port.
	"""

	ESC = 0x1b
	ACK = 'A'
	NACK = 'N'
	ETX = 0x03

	def __init__(self, port=None):
		"""Open a connection to a Blinkenhub.

		Args:
			port (str, optional): Serial device path, such as
				``/dev/cu.usbmodem1101``. If omitted, the first USB serial port
				whose manufacturer is ``Systemix`` and product is ``Blinkenhub``
				is used.

		Raises:
			Exception: If ``port`` is omitted and no Blinkenhub is found.
			serial.SerialException: If the serial port cannot be opened.
		"""
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
		"""Frame a raw payload for transmission.

		Appends a CRC-8 checksum, escapes every ``ESC`` and ``ETX`` byte by
		prefixing it with ``ESC``, and terminates the frame with ``ETX``.

		Args:
			buf (bytes): The serialized command.

		Returns:
			bytearray: The framed bytes, ready to write to the serial port.
		"""
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
		"""Send a command to the device.

		Args:
			item (BlinkenhubCmd): The command to send. Any object that supports
				``bytes()`` is accepted.
			reply (bool): If ``True`` (the default), wait for the device's
				ACK/NACK reply.

		Returns:
			bool: ``True`` if the device acknowledged the command, or if
			``reply`` is ``False``. ``False`` if the device replied with
			anything other than ACK.

		Raises:
			TimeoutException: If ``reply`` is ``True`` and no reply arrives
				within the serial timeout (0.5 s).
		"""
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
	"""Base class for all device commands.

	Each command is sent as a 4-byte header (command ID, protocol version, and
	two padding bytes) followed by a command-specific payload. Subclasses must
	set ``self.buf`` to that payload.

	Attributes:
		VERSION (int): Protocol version sent in every command header.
		CMD_SET_PIXELS (int): Command ID for :class:`SetPixels`.
		CMD_ANIMATE_HUE (int): Command ID for :class:`HueAnimation`.
		CMD_ANIMATE_SATURATION (int): Command ID for :class:`SaturationAnimation`.
		CMD_ANIMATE_VALUE (int): Command ID for :class:`ValueAnimation`.
		CMD_ROTATE (int): Command ID for :class:`Rotate`.
		CMD_SET_GAMMA (int): Command ID for :class:`SetGamma`.
		cmd (int): This command's ID.
		buf (bytes): This command's payload, set by the subclass.
	"""

	VERSION = 1

	CMD_SET_PIXELS          = 0
	CMD_ANIMATE_HUE         = 1
	CMD_ANIMATE_SATURATION  = 2
	CMD_ANIMATE_VALUE       = 3
	CMD_ROTATE              = 4
	CMD_SET_GAMMA			= 5

	def __init__(self, cmd):
		"""Initialize the command header.

		Args:
			cmd (int): One of the ``CMD_*`` command IDs.
		"""
		self.cmd = cmd

	def __bytes__(self):
		"""Serialize the command.

		Returns:
			bytearray: The 4-byte header followed by the payload in ``self.buf``.
		"""
		b = bytearray()
		b += struct.pack("<BBxx", self.cmd, self.VERSION)
		b += self.buf
		return b


class Animation(BlinkenhubCmd):
	"""Base class for commands that animate one HSV channel of every pixel.

	Use one of the subclasses: :class:`HueAnimation`,
	:class:`SaturationAnimation`, or :class:`ValueAnimation`.

	Attributes:
		ANIMATE_NONE (int): No animation.
		ANIMATE_UP (int): Ramp from ``bottom`` to ``top``.
		ANIMATE_DOWN (int): Ramp from ``top`` to ``bottom``.
		ANIMATE_UPDOWN (int): Oscillate between ``bottom`` and ``top``.
	"""

	ANIMATE_NONE = 0
	ANIMATE_UP = 1
	ANIMATE_DOWN = 2
	ANIMATE_UPDOWN = 3

	def __init__(self, cmd, typ, rate, top, bottom, count):
		"""Build an animation command.

		Args:
			cmd (int): The ``CMD_ANIMATE_*`` command ID.
			typ (int): One of the ``ANIMATE_*`` constants.
			rate (float): Speed in cycles per second.
			top (float): Upper limit of the animated channel.
			bottom (float): Lower limit of the animated channel.
			count (int): Number of cycles to run; ``-1`` runs forever.
		"""
		BlinkenhubCmd.__init__(self, cmd)
		self.buf = struct.pack("<Bxxxfffi", typ, rate, top, bottom, count)


class SetGamma(BlinkenhubCmd):
	"""Set the gamma correction the device applies to pixel brightness."""

	def __init__(self, gamma):
		"""Build a set-gamma command.

		Args:
			gamma (float): Gamma exponent; ``1.0`` means no correction.
		"""
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_SET_GAMMA)
		self.buf = struct.pack("<f", gamma)


class SetPixels(BlinkenhubCmd):
	"""Set the color of each pixel, with an optional fade.

	Attributes:
		LEN_LIMIT (int): Maximum number of pixels (the ring has 17).
	"""

	LEN_LIMIT = 17
	def __init__(self, colors, fade_time):
		"""Build a set-pixels command.

		Args:
			colors (list[HSV_Color]): One color per pixel, starting at pixel 0.
			fade_time (float): Seconds to fade from the current colors to the new
				ones; ``0`` changes them at once.

		Raises:
			Exception: If ``colors`` has more than :attr:`LEN_LIMIT` entries.
		"""
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_SET_PIXELS)
		if len(colors) > self.LEN_LIMIT:
			raise Exception("color vector too long")
		b = bytearray()
		for c in colors:
			b += c.__bytes__()
		fmt = f'<Bxxxf{len(b)}s'
		self.buf = struct.pack(fmt, len(colors), fade_time, b)


class HueAnimation(Animation):
	"""Animate the hue of every pixel."""

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		"""Build a hue animation command.

		Args:
			typ (int): One of the ``Animation.ANIMATE_*`` constants.
			rate (float): Speed in cycles per second.
			top (float): Upper hue limit in degrees, ``[0.0, 360.0]``.
			bottom (float): Lower hue limit in degrees, ``[0.0, 360.0]``.
			count (int): Number of cycles to run; ``-1`` runs forever.
		"""
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_HUE, typ, rate, top, bottom, count)


class SaturationAnimation(Animation):
	"""Animate the saturation of every pixel."""

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		"""Build a saturation animation command.

		Args:
			typ (int): One of the ``Animation.ANIMATE_*`` constants.
			rate (float): Speed in cycles per second.
			top (float): Upper saturation limit, ``[0.0, 1.0]``.
			bottom (float): Lower saturation limit, ``[0.0, 1.0]``.
			count (int): Number of cycles to run; ``-1`` runs forever.
		"""
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_SATURATION, typ, rate, top, bottom, count)


class ValueAnimation(Animation):
	"""Animate the value (brightness) of every pixel."""

	def __init__(self, typ = Animation.ANIMATE_NONE, rate = .5, top = 1, bottom = .1, count = -1):
		"""Build a value (brightness) animation command.

		Args:
			typ (int): One of the ``Animation.ANIMATE_*`` constants.
			rate (float): Speed in cycles per second.
			top (float): Upper brightness limit, ``[0.0, 1.0]``.
			bottom (float): Lower brightness limit, ``[0.0, 1.0]``.
			count (int): Number of cycles to run; ``-1`` runs forever.
		"""
		Animation.__init__(self, BlinkenhubCmd.CMD_ANIMATE_VALUE, typ, rate, top, bottom, count)


class Rotate(BlinkenhubCmd):
	"""Rotate the current pixel pattern around the ring.

	Attributes:
		NONE (int): Stop rotating.
		CW (int): Rotate clockwise.
		CCW (int): Rotate counterclockwise.
		CYLON (int): Sweep back and forth over ``limit`` pixels.
	"""

	NONE = 0
	CW = 1
	CCW = 2
	CYLON = 3

	def __init__(self, typ, rate, limit):
		"""Build a rotate command.

		Args:
			typ (int): One of :attr:`NONE`, :attr:`CW`, :attr:`CCW`, or
				:attr:`CYLON`.
			rate (float): Speed in pixels per second.
			limit (int): In :attr:`CYLON` mode, the number of pixels to sweep
				across. The other modes pass ``-1``.
		"""
		BlinkenhubCmd.__init__(self, BlinkenhubCmd.CMD_ROTATE)
		self.buf = struct.pack("<Bxxxfi", typ, rate, limit)


def mkrainbow(value):
	"""Build a rainbow that spans the full hue circle across the ring.

	Args:
		value (float): Brightness of every pixel, ``[0.0, 1.0]``.

	Returns:
		list[HSV_Color]: 17 fully saturated colors with evenly spaced hues.
	"""
	colors = []
	h = 0
	for _ in range(17):
		colors += [HSV_Color(h, 1, value)]
		h += 21.17647
	return colors


def stop_animations():
	"""Stop any rotation and channel animations running on the device.

	Connects to the port in the module-level ``device`` variable, which the CLI
	sets from ``--port``.
	"""
	bh = Blinkenhub(device)
	bh.send(Rotate(0,0,0))
	bh.send(HueAnimation(Animation.ANIMATE_UP,100,1,0,1))
	bh.send(SaturationAnimation(Animation.ANIMATE_UP,100,1,0,1))
	bh.send(ValueAnimation(Animation.ANIMATE_UP,100,1,0,1))


def mk_random_colors(value):
	"""Build a random pattern in which about half the pixels are lit.

	Args:
		value (float): Brightness of the lit pixels, ``[0.0, 1.0]``.

	Returns:
		list[HSV_Color]: 17 colors. Each is either off or a fully saturated
		random hue.
	"""
	colors = []
	for _ in range(17):
		if random() > 0.5:
			colors += [HSV_Color(random() * 360, 1, value)]
		else:
			colors +=[HSV_Color(0, 0, 0)]
	return colors


# Inspired by Claude
def are_we_awake(cg):
	"""Report whether any active display on this Mac is awake (macOS only).

	Args:
		cg (ctypes.CDLL): The loaded CoreGraphics framework, with ``restype``
			set to ``ctypes.c_int`` for ``CGDisplayIsAsleep`` and
			``CGGetActiveDisplayList``.

	Returns:
		bool: ``True`` if at least one active display is awake, otherwise ``False``.
	"""
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

def main():
	"""Entry point for the ``blinkenhub`` command-line tool.

	Builds the Click command group and runs it. The serial port can be given
	with ``--port`` or the ``BLINKENHUB_PORT`` environment variable; otherwise
	the device is detected automatically.
	"""

	import click

	@click.group()
	@click.option('--port', envvar='BLINKENHUB_PORT', help='Serial port to which Blinkenhub is connected')
	def cli(port):
		"""Control the Blinkenhub Mac mini stand."""
		global device
		device = port

	@cli.command()
	def run():
		"""Show random colors while the displays are awake, and a red Cylon sweep while they sleep (macOS only)."""
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
		"""Stop all rotation and animations."""
		stop_animations()


	@cli.command()
	@click.option('-v', '--value', default=.5, help='The value (brightness)')
	@click.option('-r', '--rate', default=0.0, help='Spin rate')
	def rainbow(value, rate):
		"""Show a rainbow, optionally spinning clockwise."""
		stop_animations()
		bh = Blinkenhub(device)
		bh.send(SetPixels(mkrainbow(value), 0))
		if (rate > 0):
			bh.send(Rotate(Rotate.CW, rate, -1))


	@cli.command()
	@click.option('-p', '--period', default=0.3, help='Time in seconds of each step')
	@click.option('-v', '--value', default=.5, help='The value (brightness)')
	def disco(period, value):
		"""Show a new random pattern every PERIOD seconds until interrupted."""
		stop_animations()
		bh = Blinkenhub(device)
		while(True):
			colors = mk_random_colors(value)
			bh.send(SetPixels(colors, 0))
			sleep(period)

	@cli.command()
	@click.argument('gamma', default=1.0)
	def set_gamma(gamma):
		"""Set the gamma correction (1.0 = none)."""
		if Blinkenhub(device).send(SetGamma(gamma)):
			click.echo('OK')
		else:
			click.echo('Error')

	@cli.command()
	@click.option('-p', '--pixel', type=(float, float, float), multiple=True)
	@click.option('-f', '--fade', default=0.0, help='Fade time in seconds)')
	@click.option('-d', '--default', type=(float, float, float), help='HSV values for the unspecified pixels')
	def set_pixels(pixel, fade, default):
		"""Set pixel colors as H S V triples.

		Each --pixel sets the next pixel, starting at pixel 0. The remaining
		pixels get the --default color, or the last --pixel color if no default
		is given.
		"""
		colors = []
		for p in pixel:
			colors += [HSV_Color(p[0], p[1], p[2])]
		if default is not None:
			d = HSV_Color(default[0], default[1], default[2])
		else:
			pi = len(pixel) - 1
			d = HSV_Color(pixel[pi][0], pixel[pi][1], pixel[pi][2])
		for _ in range(17 - len(pixel)):
			colors += [d]
		if Blinkenhub(device).send(SetPixels(colors, fade)):
			click.echo('OK')
		else:
			click.echo('Error')

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
		"""Animate the hue of every pixel."""
		if Blinkenhub(device).send(HueAnimation(typ, rate, top, bottom, count)):
			click.echo('OK')
		else:
			click.echo('Error')

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
		"""Animate the saturation of every pixel."""
		if Blinkenhub(device).send(SaturationAnimation(typ, rate, top, bottom, count)):
			click.echo('OK')
		else:
			click.echo('Error')

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
		"""Animate the value (brightness) of every pixel."""
		if Blinkenhub(device).send(ValueAnimation(typ, rate, top, bottom, count)):
			click.echo('OK')
		else:
			click.echo('Error')

	@cli.command()
	@click.option('--none', 'typ', flag_value=Rotate.NONE, default=True)
	@click.option('--cw', 'typ', flag_value=Rotate.CW)
	@click.option('--ccw', 'typ', flag_value=Rotate.CCW)
	@click.option('--cylon', 'typ', flag_value=Rotate.CYLON)
	@click.option('-r', '--rate', default=1.0, help='Rate of change as pixels per second)')
	@click.option('-l', '--limit', default=1, help='Number of pixels to rotate in cylon mode')
	def rotate(typ, rate, limit):
		"""Rotate the current pattern around the ring."""
		if Blinkenhub(device).send(Rotate(typ, rate, limit)):
			click.echo('OK')
		else:
			click.echo('Error')

	cli()

if __name__ == '__main__':

	main()
