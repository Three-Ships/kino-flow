/**
 * Screenshot capture for the website capture pipeline.
 *
 * All page.evaluate() calls use string expressions to avoid
 * tsx/esbuild __name injection (see esbuild issue #1031).
 */

import type { Page } from "puppeteer-core";
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

/**
 * Capture viewport screenshots covering the entire page height.
 *
 * Scrolls down the page in viewport-sized steps (with slight overlap),
 * taking a 1920x1080 screenshot at each position. The number of screenshots
 * depends on the page height — short pages get fewer, long pages get more.
 * Capped at 20 to avoid excessive output on extremely long pages.
 *
 * Unlike the old section-tiling approach, this does NOT disable sticky/fixed
 * elements — screenshots show the page in its natural browsing state with
 * scroll-triggered animations fired.
 */
export async function captureScrollScreenshots(page: Page, outputDir: string): Promise<string[]> {
  const screenshotsDir = join(outputDir, "screenshots");
  mkdirSync(screenshotsDir, { recursive: true });

  const MAX_SCREENSHOTS = 20;
  const filePaths: string[] = [];

  try {
    const scrollHeight = (await page.evaluate(
      `Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)`,
    )) as number;
    const viewportHeight = (await page.evaluate(`window.innerHeight`)) as number;

    // Calculate scroll positions: step by 70% of viewport (30% overlap between shots)
    const step = Math.floor(viewportHeight * 0.7);
    const positions: number[] = [0];
    for (let y = step; y < scrollHeight - viewportHeight; y += step) {
      positions.push(y);
    }
    // Always include the bottom of the page
    const lastPos = Math.max(0, scrollHeight - viewportHeight);
    if (positions[positions.length - 1] !== lastPos) {
      positions.push(lastPos);
    }

    // Downsample if too many positions
    let finalPositions = positions;
    if (positions.length > MAX_SCREENSHOTS) {
      finalPositions = [positions[0]!];
      const stride = (positions.length - 1) / (MAX_SCREENSHOTS - 1);
      for (let i = 1; i < MAX_SCREENSHOTS - 1; i++) {
        finalPositions.push(positions[Math.round(i * stride)]!);
      }
      finalPositions.push(positions[positions.length - 1]!);
    }

    for (let i = 0; i < finalPositions.length; i++) {
      await page.evaluate(`window.scrollTo(0, ${finalPositions[i]})`);
      await new Promise((r) => setTimeout(r, 400));

      const pct = Math.round(
        (finalPositions[i]! / Math.max(1, scrollHeight - viewportHeight)) * 100,
      );
      const filename = `scroll-${String(Math.min(pct, 100)).padStart(3, "0")}.png`;
      const filePath = join(screenshotsDir, filename);
      const buffer = await page.screenshot({ type: "png" });
      writeFileSync(filePath, buffer);
      filePaths.push(`screenshots/${filename}`);
    }

    // Reset scroll
    await page.evaluate(`window.scrollTo(0, 0)`);
    await new Promise((r) => setTimeout(r, 200));
  } catch {
    /* scroll screenshots are non-critical */
  }

  return filePaths;
}
