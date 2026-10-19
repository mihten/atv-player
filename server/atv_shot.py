#!/opt/atv-shot/venv/bin/python
"""One Apple TV screenshot over Wi-Fi, no root, no USB. Run as atvmedia (pairing record lives in
/var/lib/atv-media/pmd3, made with `pymobiledevice3 remote pair`).

pymobiledevice3's own --userspace path needs USB lockdown first; this uses the same building blocks
directly: RemotePairing over bonjour -> in-process PyTCP tunnel -> RSD -> DVT screenshot. One
connection per call, closed afterwards; nothing keeps running.

Developer disk image: on tvOS 27 the screenshot works WITHOUT mounting one (verified 2026-10-19), and
pymobiledevice3 11.36's Cryptex1 image is rejected there (cryptexd "invalid asset: ginf"). So the
image is only tried as a fallback when a capture fails, then the capture is retried once.

    atv-shot.py OUT.png            -> writes OUT.png and OUT.jpg (quality 95, same size), prints a
                                      JSON line with the result
Exit codes: 0 ok, 10 Apple TV not found/paired, 11 tunnel failed, 12 developer image, 13 capture.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HOME", "/var/lib/atv-media/pmd3")
os.environ.setdefault("XDG_DATA_HOME", "/var/lib/atv-media/pmd3")

from pymobiledevice3.remote import tunnel_service  # noqa: E402
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService  # noqa: E402
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane  # noqa: E402
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider  # noqa: E402
from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot  # noqa: E402


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


async def capture(out: Path) -> dict:
    t0 = time.monotonic()
    tunnel_service.USE_USERSPACE_TUNNEL = True
    services = await tunnel_service.get_remote_pairing_tunnel_services(bonjour_timeout=4)
    if not services:
        raise Fail(10, "Apple TV not found on the network (asleep, or pairing record missing)")
    provider, extra = services[0], services[1:]
    for s in extra:
        await s.close()
    try:
        try:
            tunnel_cm = provider.start_tcp_tunnel()
            result = await tunnel_cm.__aenter__()
        except Exception as e:
            raise Fail(11, f"tunnel failed: {e!r}")
        try:
            tun = result.client.tun
            tun.set_peer(result.address)
            async with UserspaceDialPlane(tun, result.address) as dial:
                rsd = RemoteServiceDiscoveryService((result.address, result.port), open_connection=dial.dial,
                                                    auxiliary_metadata=result.auxiliary_metadata)
                await rsd.connect()
                mounted = False
                try:
                    try:
                        png = await take(rsd)
                    except Exception as first:
                        mounted = await ensure_developer_image(rsd, first)
                        try:
                            png = await take(rsd)
                        except Exception as e:
                            raise Fail(13, f"screenshot failed: {e!r} (first try: {first!r})")
                finally:
                    await rsd.close()
        finally:
            await tunnel_cm.__aexit__(None, None, None)
    finally:
        await provider.close()
    tmp = out.with_name(f".{out.name}.part")
    tmp.write_bytes(png)
    tmp.replace(out)
    jpg_path, jpg_bytes = write_jpeg(png, out.with_suffix(".jpg"))
    return {"ok": True, "file": str(out), "bytes": len(png), "jpg": jpg_path, "jpg_bytes": jpg_bytes,
            "seconds": round(time.monotonic() - t0, 1), "mounted_developer_image": mounted}


def write_jpeg(png: bytes, path: Path):
    """JPEG copy (quality 95, full resolution) next to the PNG; the PNG stays the lossless original.
    A failure here never loses the PNG."""
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(BytesIO(png)).convert("RGB")
        tmp = path.with_name(f".{path.name}.part")
        # Quality 95 (user feedback, 2026-10-19: 80 showed banding in dark gradients; 95 looked fine in
        # IrfanView, 100 is ~3x larger). Chroma subsampling stays 4:2:0 (Pillow/IrfanView default).
        img.save(tmp, "JPEG", quality=95, subsampling=2, optimize=True)
        tmp.replace(path)
        return str(path), path.stat().st_size
    except Exception as e:  # noqa: BLE001
        print(f"jpeg copy failed: {e!r}", file=sys.stderr)
        return None, None


async def take(rsd) -> bytes:
    async with DvtProvider(rsd) as dvt, Screenshot(dvt) as shot:
        return await shot.get_screenshot()


async def ensure_developer_image(rsd, first_error) -> bool:
    """Fallback after a failed capture: mount the developer disk image. Returns True if mounted now."""
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, auto_mount
    try:
        async with MobileImageMounterService(rsd) as mounter:
            images = await mounter.copy_devices()
        if images:
            return False
    except Exception:
        pass  # listing is best effort; try to mount
    try:
        await auto_mount(rsd)
    except Exception as e:
        if "already mounted" in str(e).lower():
            return False
        raise Fail(12, f"screenshot failed ({first_error!r}) and the developer disk image could not be mounted: {e!r}")
    return True


def main():
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    out = Path(sys.argv[1])
    try:
        info = asyncio.run(asyncio.wait_for(capture(out), timeout=120))
        print(json.dumps(info))
    except Fail as e:
        print(json.dumps({"ok": False, "code": e.code, "error": str(e)}))
        sys.exit(e.code)
    except asyncio.TimeoutError:
        print(json.dumps({"ok": False, "code": 13, "error": "timed out after 120 s"}))
        sys.exit(13)


if __name__ == "__main__":
    main()
