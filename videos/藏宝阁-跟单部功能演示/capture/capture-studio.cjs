const { chromium } = require("playwright");
const path = require("node:path");

const projectUrl = "http://127.0.0.1:3018/#project/%E8%97%8F%E5%AE%9D%E9%98%81-%E8%B7%9F%E5%8D%95%E9%83%A8%E5%8A%9F%E8%83%BD%E6%BC%94%E7%A4%BA";
const outputPath = path.resolve("videos/藏宝阁-跟单部功能演示/studio-preview.png");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(projectUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(10000);
  const buttons = await page.locator("button").evaluateAll((nodes) => nodes.map((node) => ({
    text: node.textContent.trim(),
    ariaLabel: node.getAttribute("aria-label"),
    title: node.getAttribute("title"),
  })));
  const playButton = page.locator('button[aria-label*="Play" i], button[title*="Play" i]').first();
  if (await playButton.count()) await playButton.click();
  else await page.mouse.click(780, 615);
  await page.waitForTimeout(2500);
  await page.screenshot({ path: outputPath, fullPage: true });
  console.log(JSON.stringify({ title: await page.title(), errors, buttons }, null, 2));
  await browser.close();
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
