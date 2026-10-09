# Tuning a Raspberry Pi for glitch free USB audio

Written while making a Pi 1B play without dropouts. Pi 3 and newer usually need none of
this. Numbers below are from that board with a Behringer UMC204HD.

## Why small Pis struggle

On Pi 1, 2 and 3 the USB controller is driven by the CPU: audio packets have to be
scheduled every 125 microseconds, whatever else the system is doing. On Pi 1 and 2 a
Wi-Fi dongle and the Ethernet chip sit on that same bus, so their traffic competes with
audio. A missed packet is not a click in the usual sense: the tone wobbles, because the
device repeats or drops a fragment.

The ALSA buffer does not help here. It protects against the application being late, not
against the bus being late, which is why bigger buffers change nothing.

## What actually helped, in order of effect

| Lever | Effect |
|---|---|
| `options snd-usb-audio lowlatency=0` in `/etc/modprobe.d/` | longer USB transfer queue, the single biggest software win |
| Overclock: `arm_freq=950`, `core_freq=450`, `over_voltage=6`, `force_turbo=1` | more headroom for the CPU driven USB scheduling |
| Unbind the Ethernet driver, remove the Wi-Fi dongle if you can use Ethernet | fewer competitors on the bus |
| Play a local file instead of a network stream | the radio is silent while the track plays |
| MPD over a UNIX socket with a local file, no HTTP and no curl | fewer copies and wakeups per second |
| Disable GPU, HDMI, camera, analog audio | fewer interrupts and less memory traffic |
| 48 kHz instead of 44.1 kHz | even packet sizes, sometimes helps, measure it |

Measured on a Pi 1B: 3 to 8 audible defects per minute before, zero after.

## Diagnosing honestly

1. **Use a steady tone, not music.** A 441 Hz sine makes every defect obvious. Generate
   one by repeating a single 100 sample period.
2. **Split the chain and listen to each stage for a minute**: `aplay file`, then
   `curl | aplay`, then MPD over HTTP, then MPD from a local file. This tells you whether
   the hardware, the network, HTTP or the player adds the defect.
3. **Run a phase test**: silence, network download, silence, USB read from a flash
   drive, CPU load, silence. Thirty seconds each, printed to the terminal. This separates
   radio from bus traffic from CPU.
4. **Check the objective counters.** `aplay` without `-q` prints `underrun!!!` and the
   kernel logs URB errors in `dmesg`. If both are silent and you still hear defects, the
   problem is below the driver: the bus itself, power, or the DAC. Stop increasing
   buffers at that point.
5. **Record the output and compare with the source** if you want to judge the DAC. Always
   measure in the same sample rate you listen at, otherwise the result is meaningless.

## Other things that bite

- Address ALSA cards by name (`plughw:CARD=U192k,DEV=0`), never by number.
- A bus powered interface plus a Wi-Fi dongle can brown out a board with a weak supply.
  A powered hub is the quick test.
- `/dev/shm` and `/tmp` are cleared on reboot, and a cache that cleans up after itself
  can delete your test files mid test.
- On armv6 the Python interpreter takes seconds to start, so test scripts must wait for
  readiness rather than sleeping a fixed second.
- A loose 3.5 mm plug sounds exactly like "half the frequencies are gone": with the
  ground not connected you hear the difference between channels.
