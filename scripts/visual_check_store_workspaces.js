const { chromium } = require("playwright");

const baseUrl = process.env.REPLENISH_URL || "http://127.0.0.1:8877";
const outputDir = process.env.REPLENISH_SCREENSHOTS || "screenshots";

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath:
      process.env.CHROME_PATH ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
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

  await page.goto(`${baseUrl}/dashboard?store=TMALL-MTN-FLAGSHIP`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/tmall-workspace-desktop.png`,
    fullPage: true,
  });
  const tmallDesktop = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    activeStore: document
      .querySelector(".store-tabs a.active strong")
      ?.textContent?.trim(),
    storeCount: document.querySelectorAll(".store-tabs a").length,
    frequencyVisible: document.body.textContent.includes("修改频率"),
    apiStatus: document.querySelector(".task-band .status")?.textContent?.trim(),
  }));

  await page.goto(`${baseUrl}/data/tmall-api?store=TMALL-MTN-FLAGSHIP`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/tmall-api-config-desktop.png`,
    fullPage: true,
  });
  const tmallApiDesktop = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    syncDisabled: document
      .querySelector("button[disabled]")
      ?.textContent?.includes("同步并生成补货批次"),
    credentialFields: ["app_key", "app_secret", "session_key"].every((name) =>
      document.querySelector(`[name="${name}"]`),
    ),
  }));

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseUrl}/dashboard?store=TMALL-MTN-FLAGSHIP`, {
    waitUntil: "networkidle",
  });
  const tmallMobile = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    activeStore: document
      .querySelector(".store-tabs a.active strong")
      ?.textContent?.trim(),
    storeCount: document.querySelectorAll(".store-tabs a").length,
    tabsScrollable:
      document.querySelector(".store-tabs").scrollWidth >
      document.querySelector(".store-tabs").clientWidth,
    activeTabVisible: (() => {
      const tabs = document.querySelector(".store-tabs");
      const active = document.querySelector(".store-tabs a.active");
      return active.offsetLeft >= tabs.scrollLeft - 1 &&
        active.offsetLeft + active.offsetWidth <= tabs.scrollLeft + tabs.clientWidth + 1;
    })(),
  }));
  await page.screenshot({
    path: `${outputDir}/tmall-workspace-mobile.png`,
    fullPage: true,
  });

  await page.goto(`${baseUrl}/data/tmall-api?store=TMALL-MTN-FLAGSHIP`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/tmall-api-config-mobile.png`,
    fullPage: true,
  });
  const tmallApiMobile = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
  }));

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseUrl}/dashboard?store=VIP-BNX`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/bnx-workspace-desktop.png`,
    fullPage: true,
  });
  const bnxDesktop = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    activeStore: document
      .querySelector(".store-tabs a.active strong")
      ?.textContent?.trim(),
    storeCount: document.querySelectorAll(".store-tabs a").length,
    frequencyVisible: document.body.textContent.includes("修改频率"),
    browserCaptureVisible: document.body.textContent.includes("浏览器报表采集"),
    apiStatus: document.querySelector(".task-band .status")?.textContent?.trim(),
  }));

  await page.goto(`${baseUrl}/data/api?store=VIP-BNX`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/bnx-api-config-desktop.png`,
    fullPage: true,
  });
  const bnxApiDesktop = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
    storeCode: document.querySelector('input[name="store_code"]')?.value,
    syncDisabled: Array.from(document.querySelectorAll("button[disabled]")).some((button) =>
      button.textContent.includes("同步并生成补货批次"),
    ),
    credentialFields: ["app_key", "app_secret", "access_token"].every((name) =>
      document.querySelector(`[name="${name}"]`),
    ),
  }));

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseUrl}/dashboard?store=VIP-BNX`, {
    waitUntil: "networkidle",
  });
  const bnxMobile = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    activeStore: document
      .querySelector(".store-tabs a.active strong")
      ?.textContent?.trim(),
    storeCount: document.querySelectorAll(".store-tabs a").length,
    tabsScrollable:
      document.querySelector(".store-tabs").scrollWidth >
      document.querySelector(".store-tabs").clientWidth,
    activeTabVisible: (() => {
      const tabs = document.querySelector(".store-tabs");
      const active = document.querySelector(".store-tabs a.active");
      return active.offsetLeft >= tabs.scrollLeft - 1 &&
        active.offsetLeft + active.offsetWidth <= tabs.scrollLeft + tabs.clientWidth + 1;
    })(),
  }));
  await page.screenshot({
    path: `${outputDir}/bnx-workspace-mobile.png`,
    fullPage: true,
  });

  await page.goto(`${baseUrl}/data/api?store=VIP-BNX`, {
    waitUntil: "networkidle",
  });
  await page.screenshot({
    path: `${outputDir}/bnx-api-config-mobile.png`,
    fullPage: true,
  });
  const bnxApiMobile = await page.evaluate(() => ({
    viewport: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    title: document.querySelector("h1")?.textContent?.trim(),
  }));

  await browser.close();
  const metrics = {
    tmallDesktop,
    tmallApiDesktop,
    tmallMobile,
    tmallApiMobile,
    bnxDesktop,
    bnxApiDesktop,
    bnxMobile,
    bnxApiMobile,
    errors,
  };
  if (Object.values(metrics).some((entry) => entry?.bodyWidth > entry?.viewport)) {
    throw new Error(`Page overflow: ${JSON.stringify(metrics)}`);
  }
  if (
    tmallDesktop.storeCount !== 3 ||
    tmallDesktop.activeStore !== "马天奴天猫官方旗舰店" ||
    tmallMobile.activeStore !== "马天奴天猫官方旗舰店" ||
    tmallMobile.storeCount !== 3 ||
    !tmallMobile.tabsScrollable ||
    !tmallMobile.activeTabVisible ||
    tmallDesktop.frequencyVisible
  ) {
    throw new Error(`Store workspace state is incorrect: ${JSON.stringify(metrics)}`);
  }
  if (
    tmallApiDesktop.title !== "天猫 API 配置与试连" ||
    !tmallApiDesktop.syncDisabled ||
    !tmallApiDesktop.credentialFields
  ) {
    throw new Error(`Tmall API page is incomplete: ${JSON.stringify(metrics)}`);
  }
  if (
    bnxDesktop.storeCount !== 3 ||
    bnxDesktop.activeStore !== "BNX唯品会" ||
    bnxDesktop.frequencyVisible ||
    bnxDesktop.browserCaptureVisible ||
    bnxMobile.activeStore !== "BNX唯品会" ||
    bnxMobile.storeCount !== 3 ||
    !bnxMobile.tabsScrollable ||
    !bnxMobile.activeTabVisible
  ) {
    throw new Error(`BNX workspace state is incorrect: ${JSON.stringify(metrics)}`);
  }
  if (
    bnxApiDesktop.title !== "BNX唯品会 API 配置" ||
    bnxApiDesktop.storeCode !== "VIP-BNX" ||
    !bnxApiDesktop.syncDisabled ||
    !bnxApiDesktop.credentialFields
  ) {
    throw new Error(`BNX API page is incomplete: ${JSON.stringify(metrics)}`);
  }
  if (errors.length) throw new Error(`Browser errors: ${errors.join(" | ")}`);
  process.stdout.write(JSON.stringify(metrics, null, 2));
})();
