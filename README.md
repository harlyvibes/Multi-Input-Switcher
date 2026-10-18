# Multi-Input-Switcher

A small desktop app for switching inputs on a Samsung SmartThings TV (built for the Q90R / Q90T) using the [SmartThings REST API](https://developer.smartthings.com/docs/api/public), and, on Windows, the inputs and power of your computer monitors over DDC/CI. It opens a window with one button per TV input and adds an icon to the system tray (Windows/Linux) or menu bar (macOS), so you can pick a TV or monitor, switch inputs, or turn them on and off without opening the window.

Works on Windows, macOS and Linux, using only the Python standard library (plus two optional packages for the tray icon). The TV side works from anywhere with internet access, not just your home network. Monitor control is Windows only.

## Requirements

- Python 3.10 or newer with Tkinter (included with the python.org installers). **On macOS, do not use the system Python at `/usr/bin/python3`** — it bundles an old Tcl/Tk with serious display bugs on modern macOS (windows can render completely blank or refuse to resize, especially in Dark Mode). Install Python from [python.org/downloads/macos](https://www.python.org/downloads/macos/) (or `brew install python-tk`) and run the script with that `python3` instead.
- A SmartThings account with your TV added to it
- Optional, for the tray / menu bar icon: `pystray` and `pillow`. If they're missing when the app starts, it offers to install them into the Python that's running it.
- For monitors: Windows 10 or 11, and monitors with **DDC/CI enabled** (see [Monitors](#monitors-windows))

```sh
pip install -r requirements.txt   # optional: tray / menu bar icon
pythonw multi_input_switcher.pyw     # Windows: or just double-click the file (no console window)
python3 multi_input_switcher.pyw     # macOS
```

## Signing in

The first time you run the app, it opens **Account…**. Choose one of two ways to sign in.

### Option 1: Personal access token (quickest)

1. Go to [account.smartthings.com/tokens](https://account.smartthings.com/tokens) and create a token. Under **Devices**, tick *List all devices*, *See all devices* and *Control all devices*.
2. Paste the token into the app and click **Save**.

SmartThings tokens created after 30 December 2024 expire after 24 hours, so you'll need to paste a new one each day. The app tells you when it has expired. For a sign-in that lasts, use Option 2.

### Option 2: OAuth app (renews itself)

This needs a one-time setup with the SmartThings CLI to register your own OAuth app. After that the app refreshes its own sign-in, and you won't need the CLI again.

1. Install the [SmartThings CLI](https://github.com/SmartThingsCommunity/smartthings-cli) and run:
   ```sh
   smartthings apps:create
   ```
2. Choose **OAuth-In App**. Give it any display name, and when asked:
   - **Scopes:** `r:devices:*` and `x:devices:*`
   - **Redirect URI:** `https://httpbin.org/get`
3. Copy the **OAuth Client ID** and **Client Secret** it prints. The secret is shown only once.
4. In the app, open **Account…**, choose **OAuth app**, enter the client ID and secret, and click **Sign in…**.
5. Sign in to Samsung in the browser and approve access. You'll land on an httpbin page whose address contains `?code=…`. Copy the whole address, paste it into the app, and click **Save**.

The sign-in lasts as long as the app is used at least once every 30 days. While it's running in the tray it keeps the sign-in fresh on its own.

If you'd rather not paste the code, register the redirect URI as `http://localhost:53682/callback` instead and enter the same address in the app. The app then catches the sign-in itself. Some SmartThings accounts only accept https redirect URIs; if yours rejects it, use the httpbin one.

## Using it

The app starts hidden in the tray (it only opens a window by itself on first run, to sign in, or if the tray icon isn't available). Right-click the tray icon and choose **Show window** to open it. Only one copy runs at a time on Windows; starting a second one just exits. The tray menu's **Open log** opens `~/.multi_input_switcher.log`, which collects errors from the windowless process and what the app is doing.

The app used to be called TV Input Switcher. On first start it moves its old settings, monitor config and Start with Windows entry to the new names, so nothing resets.

Hovering over the tray icon shows the TV's current input and each monitor's current input (or "Off").

The app lists every device on your SmartThings account that supports input switching. Pick your TV, then click an input; the active one is marked with ●. Right-click the tray icon (or click the menu bar icon on macOS) for the same controls, plus the TV list itself, right at the top of the menu. Closing the window hides it to the tray; choose **Quit** from the icon's menu to exit. Your selected TV and sign-in are saved in `~/.multi_input_switcher.json`. That file contains your tokens, so keep it private.

The TV must be on to change inputs. For **Power On** to work, turn on **Power On with Mobile** (or IP Remote / Network Standby) in the TV's settings.

The terminal window used to launch the app hides itself shortly after startup (on macOS, only if it's Terminal.app — it's minimized, not closed).

### One-click switching

Right-click the tray icon → **Default switch device** to pick one input as your default (for whichever TV is currently selected). After that, just clicking the tray icon — no menu needed — switches straight to it. **Show window** moves into the right-click menu so the window is still one click away when you need it.

## Monitors (Windows)

On Windows the app finds your monitors, reads which inputs each one has, and lets you switch between them (DisplayPort, HDMI, USB-C, …) or turn them on and off from the tray menu, with no need for the monitor's buttons and on-screen menu. It talks to the monitors over **DDC/CI** using the Windows Monitor Configuration API (`dxva2.dll`), so no drivers or vendor software are needed.

```
 ┌──────────────────────────────────────────┐
 │ Switch to HDMI 1                         │   ← the default switch device
 ├──────────────────────────────────────────┤
 │ ● Living Room TV                         │
 ├──────────────────────────────────────────┤
 │ Default switch device                  ▸ │
 │ Input                                  ▸ │
 ├──────────────────────────────────────────┤
 │ Power On / Power Off / Refresh / Reload  │   ← the TV
 ├──────────────────────────────────────────┤
 │ DELL U2720Q (1)       DisplayPort 1    ▸ │──┐ ● DisplayPort 1
 │ DELL U2720Q (2)       Off              ▸ │  │   HDMI 1
 │ Built-in  (no DDC/CI)                    │  │   USB-C
 ├──────────────────────────────────────────┤  │ ─────────────
 │ Power on all monitors                    │  │   Power on
 │ Power off all monitors                   │  └   Power off
 │ Refresh monitors                         │
 ├──────────────────────────────────────────┤
 │ Master… / Network devices…               │
 ├──────────────────────────────────────────┤
 │ Edit monitor config…                     │
 │ ✓ Start with Windows                     │
 │ Open log                                 │
 ├──────────────────────────────────────────┤
 │ Show window / Quit                       │
 └──────────────────────────────────────────┘
```

- Detects every connected monitor and the inputs it reports, and marks the current one.
- One click switches input. With a single monitor, its inputs are listed directly in the menu instead of in a submenu.
- Turns monitors on and off, one at a time or all at once (see [Power on and off](#power-on-and-off)).
- Picks up input and power changes made with the monitor's own buttons by polling every 30 seconds (configurable).
- Lets you rename inputs ("Work laptop", "Gaming PC"), hide inputs you don't use, or add inputs a monitor doesn't report.
- **Start with Windows** adds an entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` that runs the file with `pythonw.exe`. If you move the file, turn the option off and on again.
- Has a [command line mode](#command-line) for scripts and hotkey tools such as AutoHotkey.

Monitors need **DDC/CI enabled**, usually a setting in the monitor's on-screen menu, often under "Other" or "System". Most external monitors support it, but laptop panels and most TVs don't.

Clicking the tray icon switches to your default switch device (see [One-click switching](#one-click-switching)); the monitor menu is in the right-click menu along with everything else.

### Power on and off

**Power on** and **Power off** are in each monitor's menu. With two or more monitors there are also **Power on all monitors** and **Power off all monitors**. They use the monitor's DDC/CI power mode setting (VCP code `0xD6`).

Monitors can support several "off" modes. The app uses the first one the monitor lists, in this order:

| Code | Mode | Can be turned back on from the app? |
| --- | --- | --- |
| `0x04` | Off (DPM) | Usually |
| `0x03` | Suspend | Usually |
| `0x02` | Standby | Usually |
| `0x05` | Off, same as the power button | Rarely |

Turning **off** works on most monitors. Turning back **on** only works if the monitor keeps listening for DDC/CI while it's off, and many don't. If **Power on** fails, use the monitor's power button. You can test a monitor with `--power N off` and then `--power N on`.

If "off" only dims the monitor or does nothing, set `power_off` for it in the config (for example `"0x05"`).

### Monitor configuration

Choose **Edit monitor config…** in the tray menu. The first time, this creates `%APPDATA%\MultiInputSwitcher\monitors.json` pre-filled with your detected monitors and their inputs. The app reloads the file automatically when you save it. (If you used Monitor Changer before, its `%APPDATA%\MonitorChanger\config.json` is copied here the first time, so your labels carry over.)

```json
{
  "poll_seconds": 30,
  "hide_unsupported": false,
  "monitors": {
    "DELL U2720Q": {
      "label": "Left monitor",
      "inputs": {
        "0x0F": "Desktop",
        "0x1B": "Work laptop",
        "0x11": "PS5"
      },
      "hide": ["0x12"],
      "power_off": "0x04"
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `poll_seconds` | How often to re-read the current inputs and power state (minimum 5). Set it to `0` to turn polling off. |
| `hide_unsupported` | Leave monitors without DDC/CI, such as a laptop panel, out of the menu. |
| `monitors.<name>` | Settings for a monitor, keyed by the name shown in the menu (not case-sensitive). The settings apply to every monitor with that name. |
| `label` | The name to show in the menu instead of the monitor's own name. |
| `inputs` | Renames inputs. It also adds any listed input the monitor didn't report, which helps with monitors whose capabilities string is incomplete. |
| `hide` | Input codes to leave out of the menu. |
| `power_off` | The power mode value **Power off** sends (see [Power on and off](#power-on-and-off)). It also adds the power items for a monitor that doesn't advertise power control. |

Input codes use the MCCS standard. The table below lists them, and `--list` shows what each monitor reports.

| Code | Input | Code | Input |
| --- | --- | --- | --- |
| `0x01` | VGA 1 | `0x0F` | DisplayPort 1 |
| `0x03` | DVI 1 | `0x10` | DisplayPort 2 |
| `0x04` | DVI 2 | `0x11` | HDMI 1 |
| `0x0C` | Component 1 | `0x12` | HDMI 2 |
| | | `0x1B` | USB-C (vendor extension) |

### Command line

```bash
python multi_input_switcher.pyw --list
```

```
1: DELL U2720Q  (\\.\DISPLAY1, primary)
     0x0F  DisplayPort 1  <- current
     0x11  HDMI 1
     0x1B  USB-C
     power: on (off = 0x04)
2: LG ULTRAGEAR  (\\.\DISPLAY2)
     0x0F  DisplayPort 1
     0x11  HDMI 1  <- current
     0x12  HDMI 2
     power: on (off = 0x05)
```

```bash
python multi_input_switcher.pyw --set 2 dp1
python multi_input_switcher.pyw --power all off
```

`MONITOR` is the monitor's number from `--list`, part of its name, or `all`. Use `python`, not `pythonw`, so the output is visible.

- `--set MONITOR INPUT`: the input can be a name (`hdmi1`, `HDMI 2`, `dp1`, `displayport2`, `usbc`, `vga1`, `dvi1`) or a raw code (`0x11`, `17`).
- `--power MONITOR on|off`: turns monitors on or off. "Off" reads each monitor's capabilities first to pick the right value, which takes a second or two.

Use these to bind inputs or power to global hotkeys with AutoHotkey or PowerToys.

## Keeping multiple computers in sync

If you run this app on more than one computer on the same network (e.g. a desktop and a laptop), only one sign-in needs to stay fresh. Right-click the tray icon (or open **Network…**) and turn on **Master** on whichever computer you want to own the SmartThings sign-in; it will refresh its tokens as usual and push them out to the other computers whenever they change.

On every *other* computer, open **Network…** and add the master's IP address to the device list (no need to turn master mode on there — every instance is always listening for a push, whether or not it's the master).

This sync is **plain, unencrypted JSON over TCP** on port `53934` — there's no login, no TLS, nothing standing between a message and being applied. That's a deliberate simplicity trade-off for a trusted home network; don't expose that port to the internet, and don't run master mode on a network you don't trust.

**Network…** also has debugging tools: it shows whether this computer is actually listening for pushes, when it last received one and from whom, and three buttons —

- **Check network**: pings every device in the list and reports back online/unreachable (and whether each one is currently the master).
- **Push now**: sends this computer's sign-in to every device immediately, regardless of master mode. Useful for testing the connection without waiting for a token refresh.
- **Request now**: asks every device to send its sign-in back to us right now, instead of waiting. Only a device with **Master** turned on will actually reply.

## How it works

The app calls the SmartThings API over HTTPS:

| Action | Request |
| --- | --- |
| Find TVs | `GET /v1/devices` (keeps devices with `mediaInputSource` or `samsungvd.mediaInputSource`) |
| Read inputs and state | `GET /v1/devices/{id}/status` |
| Switch input | `POST /v1/devices/{id}/commands` with `setInputSource` |
| Power on / off | `POST /v1/devices/{id}/commands` with `switch` `on` / `off` |
| OAuth sign-in and refresh | `POST /oauth/token` |

Monitors don't use SmartThings at all. They're found with the Windows display APIs and controlled over DDC/CI with `dxva2.dll` through `ctypes`: input select is VCP code `0x60`, power mode is `0xD6`, and the list of supported inputs comes from the monitor's capabilities string. Every DDC/CI call goes over the I2C bus in the video cable and can take a while, so they all run one at a time on a single worker thread, never the window's.

The tray icon runs in its own helper process, because macOS requires both Tkinter and the menu bar icon to own the main thread of their process. The window and tray exchange state and commands over a pair of queues; the monitor state is sent along with the TV's, and clicks on the monitor menu come back as commands. On Windows the tray icon also takes menu updates from other threads without disturbing a menu you have open.

## Troubleshooting

- **"SmartThings rejected the sign-in":** your personal access token has expired, or your OAuth sign-in lapsed. Open **Account…** and sign in again.
- **No TVs found:** make sure the TV appears in the SmartThings app on your phone under the same Samsung account.
- **Inputs missing or out of date:** SmartThings may not update while the TV is off. Turn it on and click **Refresh**.
- **Blank window on macOS, especially if it also won't resize:** you're almost certainly running macOS's built-in Python (`/usr/bin/python3`), which bundles Tk 8.5 — it predates Dark Mode and has serious, unfixable-from-inside-the-app display bugs on modern macOS. Check with `which python3`; if it prints `/usr/bin/python3`, install Python from [python.org/downloads/macos](https://www.python.org/downloads/macos/) and run the script with that `python3` instead. The app also prints a warning to the terminal on startup if it detects this.
- **No tray icon / menu bar icon:** if `pystray`/`pillow` aren't installed for the Python you're running the app with, it offers to install them and restart. This is especially easy to hit right after switching to a different Python (e.g. moving off `/usr/bin/python3` per the item above) — packages installed for the old interpreter don't carry over. To do it by hand, run `pip install -r requirements.txt` using that *same* `python3`/`python`, then restart the app. If the packages are installed and the icon still doesn't show, check `~/.multi_input_switcher_tray_error.log` for a crash traceback from the tray's helper process.
- **"(no DDC/CI)" next to a monitor:** turn on DDC/CI in the monitor's on-screen menu, then choose **Refresh monitors**. Some docks, KVM switches and adapters don't pass DDC/CI through, so try a direct cable. Laptop panels, virtual machines and remote desktop sessions don't support DDC/CI.
- **The monitor stops responding after switching away:** some monitors only answer DDC/CI on the active input, so a PC that isn't being shown can't switch it back. Switch back from the device that is being shown, or use the monitor's buttons.
- **Monitor power on doesn't work, or the monitor shows "(no DDC/CI)" while it's off:** the monitor stops listening for DDC/CI when it's off. Turn it on with its power button. DisplayPort monitors that disconnect when off also disappear from Windows, so the app can't reach them until they're back on.
- **A monitor input is missing or has a strange name:** some monitors report incomplete or non-standard capabilities. Add the input under `inputs` in the monitor config. Some LG models use non-standard codes, and those may not work.
- **Something else:** choose **Open log** in the tray menu.

## Development

Run the tests from the repository folder. They never touch a real monitor or the network, so they run on any platform:

```bash
python -m unittest discover -s tests -v
```

`multi_input_switcher.pyw` is a single file, split into sections: SmartThings (config, HTTP, auth, API), local network sync, monitors (MCCS parser, Windows API, DDC/CI, config, start with Windows, manager, command line), the tray process, the dialogs and main window, and the entry point. The Windows-only API code sits behind a platform check, so the file imports on macOS and Linux too.
