import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from services.chatgpt_core.browser_identity import (
    CAMOUFOX_CONTEXT_SETTERS,
    generate_browser_fingerprint,
)
from services.chatgpt_core.shared_camoufox import shared_camoufox_registration_session
from services.chatgpt_core.shared_camoufox import camoufox_executable_options


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CAMOUFOX_CONTEXT_INTEGRATION") != "1",
    reason="set RUN_CAMOUFOX_CONTEXT_INTEGRATION=1 in the isolated browser image",
)


_OBSERVE_SCRIPT = """
async setterNames => {
  const canvas = document.createElement('canvas');
  canvas.width = 320;
  canvas.height = 100;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#f60';
  ctx.fillRect(10, 10, 100, 50);
  ctx.font = '18px Arial';
  ctx.fillStyle = '#069';
  ctx.fillText('auto-gpt-context', 14, 78);
  const canvasValue = canvas.toDataURL();

  let audioValue = '';
  try {
    const AudioCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    const audio = new AudioCtx(1, 4096, 44100);
    const oscillator = audio.createOscillator();
    const compressor = audio.createDynamicsCompressor();
    oscillator.type = 'triangle';
    oscillator.frequency.value = 10000;
    oscillator.connect(compressor);
    compressor.connect(audio.destination);
    oscillator.start(0);
    const rendered = await audio.startRendering();
    audioValue = Array.from(rendered.getChannelData(0).slice(1000, 1256)).join(',');
  } catch (error) {
    audioValue = `error:${error && error.name}`;
  }

  let webgl = null;
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    if (gl && ext) {
      webgl = {
        vendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
        renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),
      };
    }
  } catch (_) {}

  const workerValue = await new Promise(resolve => {
    const source = `postMessage({ua:navigator.userAgent,cores:navigator.hardwareConcurrency})`;
    const worker = new Worker(URL.createObjectURL(new Blob([source], {type:'text/javascript'})));
    const timer = setTimeout(() => resolve({ua:'timeout', cores:0}), 3000);
    worker.onmessage = event => {
      clearTimeout(timer);
      worker.terminate();
      resolve(event.data);
    };
  });

  return {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    oscpu: navigator.oscpu,
    cores: navigator.hardwareConcurrency,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
    canvasValue,
    audioValue,
    webgl,
    workerValue,
    settersVisible: setterNames.filter(name => typeof window[name] !== 'undefined'),
  };
}
"""


def _observe(page) -> dict:
    value = page.evaluate(_OBSERVE_SCRIPT, list(CAMOUFOX_CONTEXT_SETTERS))
    value["canvasHash"] = hashlib.sha256(
        value.pop("canvasValue").encode("utf-8")
    ).hexdigest()
    value["audioHash"] = hashlib.sha256(
        value.pop("audioValue").encode("utf-8")
    ).hexdigest()
    return value


def _observe_process(profile, barrier: Barrier | None = None) -> dict:
    with shared_camoufox_registration_session(
        headless=True,
        browser_fingerprint=profile,
    ) as session:
        if barrier is not None:
            barrier.wait(timeout=30)
        first = _observe(session.page)
        repeated = _observe(session.page)
        return {
            "pid": session.process_id,
            "first": first,
            "repeated": repeated,
        }


def test_two_processes_have_stable_and_distinct_native_fingerprints():
    first_profile = generate_browser_fingerprint(
        browser_family="firefox", deep_context=True
    )
    second_profile = generate_browser_fingerprint(
        browser_family="firefox", deep_context=True
    )
    for _ in range(40):
        if (
            second_profile.webgl_vendor,
            second_profile.webgl_renderer,
            second_profile.screen_width,
            second_profile.screen_height,
        ) != (
            first_profile.webgl_vendor,
            first_profile.webgl_renderer,
            first_profile.screen_width,
            first_profile.screen_height,
        ):
            break
        second_profile = generate_browser_fingerprint(
            browser_family="firefox", deep_context=True
        )

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(_observe_process, first_profile, barrier)
        second_future = executor.submit(_observe_process, second_profile, barrier)
        first_result = first_future.result(timeout=120)
        second_result = second_future.result(timeout=120)

    first = first_result["first"]
    first_repeat = first_result["repeated"]
    second = second_result["first"]
    assert first_result["pid"] > 0
    assert second_result["pid"] > 0
    assert first_result["pid"] != second_result["pid"]

    assert first["userAgent"] == first_profile.user_agent
    assert second["userAgent"] == second_profile.user_agent
    assert first["workerValue"]["ua"] == first_profile.user_agent
    assert second["workerValue"]["ua"] == second_profile.user_agent
    for observed, profile in (
        (first, first_profile),
        (second, second_profile),
    ):
        assert observed["platform"] == profile.navigator_platform
        assert observed["oscpu"] == profile.navigator_oscpu
        assert observed["cores"] == profile.hardware_concurrency
        assert observed["workerValue"]["cores"] == profile.hardware_concurrency
        assert observed["timezone"] == profile.timezone
        assert observed["screen"] == (
            f"{profile.screen_width}x{profile.screen_height}x{profile.color_depth}"
        )
        assert observed["webgl"] == {
            "vendor": profile.webgl_vendor,
            "renderer": profile.webgl_renderer,
        }
        assert observed["settersVisible"] == []
    assert first["canvasHash"] == first_repeat["canvasHash"]
    assert first["audioHash"] == first_repeat["audioHash"]
    assert first["audioHash"] != second["audioHash"]
    assert (first["screen"], first["webgl"]) != (
        second["screen"],
        second["webgl"],
    )

    first_relaunched = _observe_process(first_profile)["first"]
    assert first_relaunched["canvasHash"] == first["canvasHash"]
    assert first_relaunched["audioHash"] == first["audioHash"]
    assert first_relaunched["screen"] == first["screen"]

    executable_options = camoufox_executable_options()
    if executable_options.get("executable_path"):
        executable = Path(executable_options["executable_path"])
    else:
        from camoufox.pkgman import launch_path

        executable = Path(launch_path())
    metadata = json.loads((executable.parent / "version.json").read_text("utf-8"))
    assert metadata["version"] == "152.0.4"
    assert metadata["release"] == "beta.28"
