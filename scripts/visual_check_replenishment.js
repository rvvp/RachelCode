const { chromium } = require("playwright");

const baseUrl = process.env.REPLENISH_URL || "http://127.0.0.1:8877";
const outputDir = process.env.REPLENISH_SCREENSHOTS || "screenshots";

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.locator('input[name="username"]').fill("merch");
  await page.locator('input[name="password"]').fill("demo123");
  await Promise.all([
    page.waitForURL("**/dashboard"),
    page.locator('button[type="submit"]').click(),
  ]);
  await page.screenshot({ path: `${outputDir}/replenishment-dashboard-desktop.png`, fullPage: true });

  const desktopMetrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    riskRows: document.querySelectorAll("tbody tr").length,
    metricLabels: Array.from(document.querySelectorAll(".metric-strip span")).map((node) => node.textContent.trim()),
    metricValues: Array.from(document.querySelectorAll(".metric-strip strong")).map((node) => node.textContent.trim()),
    conditionText: document.querySelector(".section-heading p")?.textContent?.trim(),
    conditionTags: document.querySelectorAll(".condition-tag").length,
  }));

  const taskLink = page.locator('a[href^="/plans/"]').filter({ hasText: /处理本次任务|查看本次结果/ });
  const planPath = await taskLink.getAttribute("href");
  await taskLink.click();
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${outputDir}/replenishment-plan-desktop.png`, fullPage: true });
  const planDesktopMetrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    formulaText: document.querySelector(".formula-line")?.textContent?.replace(/\s+/g, " ").trim(),
    goodsGroups: document.querySelectorAll(".goods-group").length,
    condition1Tags: document.querySelectorAll(".condition-tag.condition_1").length,
    condition2Tags: document.querySelectorAll(".condition-tag.condition_2").length,
  }));

  await page.goto(`${baseUrl}/settings`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${outputDir}/replenishment-settings-desktop.png`, fullPage: true });
  const settingsDesktopMetrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    condition1: document.querySelector('input[name="min_sales_7"]')?.value,
    condition2: document.querySelector('input[name="min_consecutive_sales_days"]')?.value,
    coverage: document.querySelector('input[name="max_coverage_days"]')?.value,
    relation: document.querySelector(".condition-or")?.textContent?.trim(),
  }));

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${outputDir}/replenishment-dashboard-mobile.png`, fullPage: true });
  const mobileMetrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    navVisible: getComputedStyle(document.querySelector(".mobile-nav")).display !== "none",
  }));
  await page.goto(`${baseUrl}${planPath}`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${outputDir}/replenishment-plan-mobile.png`, fullPage: true });
  const planMobileMetrics = await page.evaluate(() => ({ viewport: window.innerWidth, bodyWidth: document.body.scrollWidth }));
  await page.goto(`${baseUrl}/settings`, { waitUntil: "networkidle" });
  await page.screenshot({ path: `${outputDir}/replenishment-settings-mobile.png`, fullPage: true });
  const settingsMobileMetrics = await page.evaluate(() => ({ viewport: window.innerWidth, bodyWidth: document.body.scrollWidth }));

  await browser.close();
  const metrics = { desktopMetrics, planDesktopMetrics, settingsDesktopMetrics, mobileMetrics, planMobileMetrics, settingsMobileMetrics };
  if (Object.values(metrics).some((entry) => entry.bodyWidth > entry.viewport)) {
    throw new Error(`Page overflow detected: ${JSON.stringify(metrics)}`);
  }
  if (!desktopMetrics.conditionText?.includes("条件2") || !desktopMetrics.conditionTags) throw new Error("Dashboard condition display is incomplete");
  const goodsMetricIndex = desktopMetrics.metricLabels.indexOf("本期补货货号数");
  if (goodsMetricIndex === -1 || desktopMetrics.metricValues[goodsMetricIndex] !== "31") throw new Error("Dashboard goods count is incorrect");
  if (!planDesktopMetrics.formulaText?.includes("或 条件2") || !planDesktopMetrics.condition2Tags) throw new Error("Plan condition display is incomplete");
  if (settingsDesktopMetrics.condition1 !== "5" || settingsDesktopMetrics.condition2 !== "3" || settingsDesktopMetrics.coverage !== "14" || settingsDesktopMetrics.relation !== "或者") {
    throw new Error(`Settings condition values are incorrect: ${JSON.stringify(settingsDesktopMetrics)}`);
  }
  if (errors.length) throw new Error(`Browser errors: ${errors.join(" | ")}`);
  process.stdout.write(JSON.stringify(metrics, null, 2));
})();
