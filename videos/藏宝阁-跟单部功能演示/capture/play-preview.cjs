const { chromium } = require("playwright");

const projectUrl = "http://127.0.0.1:3018/#project/%E8%97%8F%E5%AE%9D%E9%98%81-%E8%B7%9F%E5%8D%95%E9%83%A8%E5%8A%9F%E8%83%BD%E6%BC%94%E7%A4%BA";

(async () => {
  const browser = await chromium.launch({
    headless: false,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
  await page.goto(projectUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(8000);
  await page.locator('button[aria-label="Play"]').click();
  await page.waitForTimeout(1500);
  console.log(await page.locator("button[aria-label=Pause]").count() ? "preview_playing" : "preview_play_request_sent");
  await new Promise(() => {});
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
