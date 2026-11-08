"""Browser regression for visible Agent mode progress and saved-file links."""

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest


class UiAgentProgressTests(unittest.TestCase):
    def test_progress_panel_keeps_steps_and_java_preview_after_completion(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable")
        root = Path(__file__).resolve().parent.parent
        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def check():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="load")
                    result = await page.evaluate("""() => {
                      const data = {mode:'agent', generation:{model:'fake', phase:'tool',
                        target:'Agent response', activity:'Tool finished: write_file'},
                        agent_steps:[{label:'Tool finished: write_file', detail:'Solution.java',
                          files:['C:/work/Solution.java'], preview:'public class Solution {}'}],
                        files:['C:/work/Solution.java']};
                      running = true; renderGeneration(data);
                      running = false; renderGeneration(data);
                      return {open: document.querySelector('#generation').open,
                        steps: document.querySelector('#agent-steps').innerText,
                        files: document.querySelector('#agent-files').innerText,
                        code: document.querySelector('#generation-output').innerText};
                    }""")
                    self.assertTrue(result["open"])
                    self.assertIn("write_file", result["steps"])
                    self.assertIn("Solution.java", result["files"])
                    self.assertIn("public class Solution", result["code"])
                finally:
                    await browser.close()

        try:
            asyncio.run(check())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
