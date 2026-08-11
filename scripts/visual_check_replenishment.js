const { chromium } = require("playwright");

const baseUrl = process.env.REPLENISH_URL || "http://127.0.0.1:8877";
const outputDir = process.env.REPLENISH_SCREENSHOTS || "screenshots";

async function login(page, username) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill("demo123");
  await Promise.all([
    page.waitForURL("**/dashboard"),
    page.locator('button[type="submit"]').click(),
  ]);
}

async function pageMetrics(page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    nav: Array.from(document.querySelectorAll(".mobile-nav a, .sidebar nav a")).map((node) => node.textContent.trim()),
  }));
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(error.message));

  await login(page, "merch");
  await page.screenshot({ path: `${outputDir}/merchandise-overview-desktop.png`, fullPage: true });
  const overview = await pageMetrics(page);
  const overviewText = await page.locator("body").innerText();
  if (!overviewText.includes("货品监控中心") || !overviewText.includes("当月单店毛利率") || !overviewText.includes("14天现货消化率")) {
    throw new Error("动销总览关键信息缺失");
  }
  if (!overviewText.includes("临时补货") || !overviewText.includes("调价工作区")) throw new Error("模块入口缺失");

  await page.goto(`${baseUrl}/plans`, { waitUntil: "networkidle" });
  const plans = await pageMetrics(page);
  await page.screenshot({ path: `${outputDir}/replenishment-workspace-desktop.png`, fullPage: true });
  if (!plans.title?.includes("补货")) throw new Error("补货工作区标题异常");

  await page.goto(`${baseUrl}/pricing`, { waitUntil: "networkidle" });
  const pricing = await pageMetrics(page);
  await page.screenshot({ path: `${outputDir}/pricing-workspace-desktop.png`, fullPage: true });
  if (!pricing.title?.includes("调价")) throw new Error("调价工作区标题异常");
  if (!((await page.locator('button:has-text("生成调价建议")').count()) > 0)) throw new Error("商品部调价控制缺失");

  await page.goto(`${baseUrl}/costs`, { waitUntil: "networkidle" });
  const costs = await pageMetrics(page);
  await page.screenshot({ path: `${outputDir}/cost-master-desktop.png`, fullPage: true });
  if (!costs.title?.includes("商品成本")) throw new Error("成本页面标题异常");

  await page.goto(`${baseUrl}/temporary-replenishment`, { waitUntil: "networkidle" });
  const temporaryText = await page.locator("body").innerText();
  if (!temporaryText.includes("临时补货")) throw new Error("临时补货入口不可用");

  const ops = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  await login(ops, "ops");
  await ops.goto(`${baseUrl}/pricing`, { waitUntil: "networkidle" });
  const opsText = await ops.locator("body").innerText();
  if (!opsText.includes("运营执行明细")) throw new Error("运营部执行视图缺失");
  if (opsText.includes("单位成本") || opsText.includes("毛利率") || opsText.includes("毛利保护价")) throw new Error("运营部页面泄露内部字段");
  await ops.screenshot({ path: `${outputDir}/pricing-operations-desktop.png`, fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "networkidle" });
  const mobile = await pageMetrics(page);
  await page.screenshot({ path: `${outputDir}/merchandise-overview-mobile.png`, fullPage: true });

  await browser.close();
  const metrics = { overview, plans, pricing, costs, mobile };
  if (Object.values(metrics).some((entry) => entry.bodyWidth > entry.viewport)) throw new Error(`Page overflow detected: ${JSON.stringify(metrics)}`);
  if (errors.length) throw new Error(`Browser errors: ${errors.join(" | ")}`);
  process.stdout.write(JSON.stringify(metrics, null, 2));
})();
