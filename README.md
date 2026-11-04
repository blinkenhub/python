# blinkenhub

Python library and command-line tool to control the [Blinkenhub](https://blinkenhub.com)
Mac mini stand, a USB-connected ring of 17 RGB LEDs.

## Installation

```sh
pip install blinkenhub
```

This installs the `blinkenhub` module and the `blinkenhub` command.

## Command-line usage

The device is found automatically. To use a specific serial port, pass
`--port` or set the `BLINKENHUB_PORT` environment variable.

```sh
blinkenhub rainbow --value 0.3 --rate 2   # spinning rainbow
blinkenhub disco                          # random colors until Ctrl-C
blinkenhub set-pixels -p 0 1 .5 -d 240 1 .2
blinkenhub animate-value --oscillate --rate 0.5 --count -1
blinkenhub stop                           # stop all animations
```

Run `blinkenhub --help`, or `blinkenhub <command> --help`, for every command
and option.

`blinkenhub run` shows random colors while the Mac's displays are awake and a
red sweep while they sleep. It works on macOS only; the other commands work on
any platform.

## Library usage

Colors are HSV: hue in degrees (0–360), saturation and value (brightness)
from 0 to 1.

```python
from blinkenhub import Blinkenhub, HSV_Color, SetPixels, Rotate, ValueAnimation, Animation

bh = Blinkenhub()            # or Blinkenhub("/dev/cu.usbmodem1101")

# Set all 17 pixels to dim blue, fading over one second
bh.send(SetPixels([HSV_Color(240, 1, 0.2)] * 17, 1.0))

# Pulse the brightness forever
bh.send(ValueAnimation(Animation.ANIMATE_UPDOWN, rate=0.5, top=0.5, bottom=0.05, count=-1))

# Spin the pattern clockwise at 3 pixels per second
bh.send(Rotate(Rotate.CW, 3.0, -1))
```

`send()` returns `True` when the device accepts the command, and raises
`TimeoutException` if it doesn't answer.

## License

MIT. See [LICENSE](LICENSE).
