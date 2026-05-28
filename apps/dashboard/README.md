# Virtual TCU - LAN Dashboard

This fork serves a lightweight Chinese read-only dashboard at `:8765`.

The dashboard is intentionally implemented as a static `index.html` instead of the original Vue/Vite browser dashboard. This keeps LAN clients such as iPad, iPhone, Mac Chrome, and other browsers from hitting the remote black-screen issue while preserving WebSocket telemetry updates.

## What It Shows

- Gear, speed, RPM, power, torque, and pedal input
- 20-segment RPM LED bar
- TCU state, airborne/yaw/watchdog flags
- Live RPM/throttle/brake/speed chart
- G-meter and grip usage
- Vehicle learning, drive style, and power-band status
- Session stats and shift history

The dashboard is display-only. Use the Electron Settings window for tuning, drive modes, network settings, output mode, and logging controls.

## Development

1. Start the backend:

   ```bash
   python -m virtual_tcu
   ```

2. Run the dashboard dev server:

   ```bash
   cd apps/dashboard
   pnpm install
   pnpm dev
   ```

3. Open `http://127.0.0.1:5173`.

Vite serves `index.html` and proxies `/ws` to `http://127.0.0.1:8765`.

## Production Build

```bash
cd apps/dashboard
pnpm install
pnpm build
```

The build writes `virtual_tcu/web/dist/index.html`. Packaged releases include this file automatically.

## LAN Use

In Virtual TCU Settings, set **Web bind address** to `0.0.0.0`, then open:

```text
http://<your-PC-LAN-IP>:8765
```

Example:

```text
http://192.168.18.58:8765
```
