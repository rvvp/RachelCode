const { chromium } = require("playwright");
const { mkdir, writeFile } = require("node:fs/promises");
const path = require("node:path");

const baseUrl = "http://127.0.0.1:8765";
const workspace = "/Users/apple/Documents/商品资料后台/videos";

const profiles = [
  {
    key: "b",
    username: "b_editor",
    outputDir: path.join(workspace, "藏宝阁-商品部功能演示/capture/screenshots/b-pages"),
    pages: [
      ["modules", "01-b-home-full.png", "/modules"],
      ["products", "02-b-products-full.png", "/products"],
      ["productEdit", "03-b-product-edit-full.png", "/products/18/edit"],
      ["billing", "04-b-billing-full.png", "/billing"],
      ["platformBills", "05-b-platform-bills-full.png", "/billing/platform-bills?month=2026-07&platform=tmall"],
      ["brandBills", "06-b-brand-bills-full.png", "/billing/brand-bills?month=2026-07"],
    ],
  },
  {
    key: "c",
    username: "d_viewer",
    outputDir: path.join(workspace, "藏宝阁-运营部功能演示/capture/screenshots/c-pages"),
    pages: [
      ["modules", "01-c-home-full.png", "/modules"],
      ["products", "02-c-products-full.png", "/products"],
      ["billing", "04-c-billing-full.png", "/billing"],
      ["platformBillsEmpty", "05-c-platform-empty-full.png", "/billing/platform-bills?month=2026-08&platform=tmall"],
      ["platformBillsSubmitted", "06-c-platform-submitted-full.png", "/billing/platform-bills?month=2026-07&platform=tmall"],
    ],
  },
];

const box = async (locator) => {
  if (!locator || !(await locator.count())) return null;
  const rect = await locator.first().boundingBox();
  if (!rect) return null;
  return Object.fromEntries(Object.entries(rect).map(([key, value]) => [key, Math.round(value * 10) / 10]));
};

const targetLocators = (page, key) => {
  const common = {
    back: page.getByRole("link", { name: "← 返回上一层" }),
  };
  const targets = {
    modules: {
      productCard: page.getByRole("heading", { name: "商品资料后台" }).locator(".."),
      productEnter: page.getByRole("link", { name: "进入板块一" }),
      billingCard: page.getByRole("heading", { name: "账单与结算" }).locator(".."),
      billingEnter: page.getByRole("link", { name: "进入板块二" }),
    },
    products: {
      workspaceCard: page.locator(".products-overview-card"),
      searchCard: page.locator("#products-search-filter"),
      statsCard: page.locator(".products-stats-panel"),
      listCard: page.locator(".products-main-stack > section.panel"),
      importExcel: page.getByRole("link", { name: "导入 Excel" }),
      importImages: page.getByRole("link", { name: "导入图片" }),
      exportMenu: page.locator(".export-menu").first(),
      bulkComplete: page.getByRole("button", { name: "批量完成给运营部" }),
      bulkReturn: page.getByRole("button", { name: "批量退回给跟单部" }),
      bulkReceive: page.getByRole("button", { name: "批量接收资料" }),
      firstCheckbox: page.locator('tbody input[name="product_ids"]').first(),
      firstReceive: page.getByRole("button", { name: "接收" }).first(),
    },
    productEdit: {
      form: page.locator("form").filter({ has: page.locator('select[name="category"]') }).first(),
      category: page.locator('select[name="category"]'),
      image: page.locator('input[name="image"]'),
      launchPrice: page.locator('input[name="launch_price"]'),
      launchChannel: page.locator('[name="launch_channel"]'),
      dataComplete: page.locator('[name="data_complete"]'),
    },
    billing: {
      monthlyBoard: page.locator("#billing-monthly-board"),
      platformCard: page.getByRole("heading", { name: "平台账单" }).locator(".."),
      brandCard: page.getByRole("heading", { name: "品牌月账单" }).locator(".."),
    },
    platformBills: {
      summary: page.getByRole("heading", { name: "平台账单" }).locator(".."),
      workspace: page.getByRole("heading", { name: /当月工作台$/ }).locator(".."),
      platformSelect: page.locator('select[name="platform"]'),
      files: page.locator(".platform-file-list").first(),
    },
    platformBillsEmpty: {
      summary: page.getByRole("heading", { name: "平台账单" }).locator(".."),
      workspace: page.getByRole("heading", { name: /当月工作台$/ }).locator(".."),
      platformSelect: page.locator('select[name="platform"]'),
      upload: page.getByRole("button", { name: "上传文件" }),
      deleteFile: page.getByRole("button", { name: "删除文件" }),
      submit: page.getByRole("button", { name: "确认提交" }),
    },
    platformBillsSubmitted: {
      summary: page.getByRole("heading", { name: "平台账单" }).locator(".."),
      workspace: page.getByRole("heading", { name: /当月工作台$/ }).locator(".."),
      platformSelect: page.locator('select[name="platform"]'),
      returnRequest: page.getByRole("button", { name: "申请退回" }),
    },
    brandBills: {
      billPanel: page.locator(".brand-bills-primary-panel"),
      status: page.getByRole("heading", { name: "当前流程状态" }).locator(".."),
      currentBill: page.getByRole("heading", { name: /当月账单$/ }).locator(".."),
      history: page.getByRole("heading", { name: "历史账单查询" }).locator(".."),
      dashboard: page.locator(".brand-dashboard-panel"),
    },
  };
  return { ...common, ...(targets[key] || {}) };
};

const login = async (page, username) => {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill("demo123");
  await Promise.all([page.waitForURL("**/modules"), page.locator('button[type="submit"]').click()]);
};

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });

  for (const profile of profiles) {
    await mkdir(profile.outputDir, { recursive: true });
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    const metadata = {};
    await login(page, profile.username);

    for (const [key, filename, route] of profile.pages) {
      await page.goto(`${baseUrl}${route}`, { waitUntil: "networkidle" });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(profile.outputDir, filename), fullPage: true });
      const targets = targetLocators(page, key);
      metadata[key] = {
        route,
        pageHeight: await page.evaluate(() => document.documentElement.scrollHeight),
        targets: {},
      };
      for (const [targetKey, locator] of Object.entries(targets)) {
        metadata[key].targets[targetKey] = await box(locator);
      }
    }

    await page.goto(`${baseUrl}/products`, { waitUntil: "networkidle" });
    if (profile.key === "b") {
      await page.evaluate(() => {
        window.scrollTo(0, 760);
        const wrap = document.querySelector(".products-list-scroll-wrap");
        if (wrap) wrap.scrollLeft = wrap.scrollWidth;
      });
      await page.waitForTimeout(250);
      await page.screenshot({ path: path.join(profile.outputDir, "02-b-products-operations.png") });
    } else {
      await page.evaluate(() => window.scrollTo(0, 520));
      await page.waitForTimeout(250);
      await page.screenshot({ path: path.join(profile.outputDir, "03-c-products-receive.png") });
      const detailHref = await page
        .locator("tr")
        .filter({ has: page.getByRole("button", { name: "接收" }) })
        .first()
        .getByRole("link", { name: "查看", exact: true })
        .getAttribute("href");
      if (detailHref) {
        await page.goto(`${baseUrl}${detailHref}`, { waitUntil: "networkidle" });
        await page.screenshot({ path: path.join(profile.outputDir, "03-c-product-detail-full.png"), fullPage: true });
        metadata.productDetail = {
          route: detailHref,
          pageHeight: await page.evaluate(() => document.documentElement.scrollHeight),
          targets: {
            detail: await box(page.getByRole("heading", { name: "资料详情" }).locator("..")),
          },
        };
      }
    }

    await writeFile(path.join(profile.outputDir, "capture-meta.json"), `${JSON.stringify(metadata, null, 2)}\n`);
    await page.close();
  }

  await browser.close();
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
