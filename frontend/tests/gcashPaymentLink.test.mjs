import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const moduleDir = await mkdtemp(join(tmpdir(), 'auto-gpt-gcash-link-tests-'))
after(() => rm(moduleDir, { force: true, recursive: true }))

async function transpile(sourceRelative, outputName, replacements = []) {
  const sourceUrl = new URL(sourceRelative, import.meta.url)
  let source = await readFile(sourceUrl, 'utf8')
  for (const [from, to] of replacements) source = source.replaceAll(from, to)
  const result = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
    fileName: sourceUrl.pathname,
  })
  await writeFile(join(moduleDir, outputName), result.outputText)
}

await transpile('../src/lib/dateTime.ts', 'dateTime.mjs')
await transpile('../src/features/accounts/gcashPaymentLink.ts', 'gcashPaymentLink.mjs', [
  ["@/lib/dateTime", './dateTime.mjs'],
])

const {
  effectiveGcashPaymentLinkExpiryMs,
  formatGcashRemainingSeconds,
  gcashPaymentLinkFromAccount,
  gcashRemainingView,
  safeGcashPaymentLinkUrl,
} = await import(pathToFileURL(join(moduleDir, 'gcashPaymentLink.mjs')).href)

test('GCash account summary only reads the dedicated payment-link marker', () => {
  const summary = gcashPaymentLinkFromAccount({
    payment_link: { url: 'https://paypal.example/ignored' },
    gcash_payment_method: { state: 'available' },
    gcash_payment_link: {
      state: 'active',
      url: 'https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=abc',
      gcash_qr_expires_at: 1_800_000_120,
      link_expires_at: 1_800_000_300,
      browser_tab_state: 'ready',
    },
  })
  assert.equal(summary.url, 'https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=abc')
  assert.equal(summary.state, 'active')
  assert.equal(summary.browserTabState, 'ready')
  assert.equal(gcashPaymentLinkFromAccount({ payment_link: { url: 'https://ignored.example' } }).url, '')
})

test('GCash URL validation accepts only the official bounded Adyen redirect', () => {
  const official = 'https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=abc'
  assert.equal(safeGcashPaymentLinkUrl(official), official)
  assert.equal(safeGcashPaymentLinkUrl('https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect'), '')
  assert.equal(safeGcashPaymentLinkUrl('https://example.com/path'), '')
  assert.equal(safeGcashPaymentLinkUrl('https://user@checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=abc'), '')
  assert.equal(safeGcashPaymentLinkUrl('https://checkoutshopper-live.adyen.com:444/checkoutshopper/checkoutPaymentRedirect?redirectData=abc'), '')
  assert.equal(safeGcashPaymentLinkUrl('http://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=abc'), '')
  assert.equal(safeGcashPaymentLinkUrl('javascript:alert(1)'), '')
  assert.equal(safeGcashPaymentLinkUrl('https:///missing-host'), '')
  assert.equal(safeGcashPaymentLinkUrl(`https://example.com/${'x'.repeat(8200)}`), '')
})

test('effective GCash expiry always chooses the earlier QR or link deadline', () => {
  const qrSeconds = 1_800_000_120
  const linkSeconds = 1_800_000_300
  assert.equal(effectiveGcashPaymentLinkExpiryMs({
    gcashQrExpiresAt: qrSeconds,
    linkExpiresAt: linkSeconds,
    effectiveExpiresAt: 1_900_000_000,
  }), qrSeconds * 1000)
  assert.equal(effectiveGcashPaymentLinkExpiryMs({
    gcashQrExpiresAt: null,
    linkExpiresAt: linkSeconds * 1000,
    effectiveExpiresAt: null,
  }), linkSeconds * 1000)
  assert.equal(effectiveGcashPaymentLinkExpiryMs({
    gcashQrExpiresAt: null,
    linkExpiresAt: null,
    effectiveExpiresAt: '2027-01-15T08:00:00Z',
  }), Date.parse('2027-01-15T08:00:00Z'))
})

test('GCash remaining-time view keeps exact warning and expiry boundaries', () => {
  const deadline = 1_800_000_000_000
  const value = { gcashQrExpiresAt: deadline, linkExpiresAt: null, effectiveExpiresAt: null }
  assert.equal(gcashRemainingView(value, deadline - 121_000).state, 'active')
  assert.deepEqual(gcashRemainingView(value, deadline - 120_000), {
    state: 'warning',
    label: '02:00',
    remainingSeconds: 120,
    expiresAtMs: deadline,
  })
  assert.equal(gcashRemainingView(value, deadline).state, 'expired')
  assert.equal(gcashRemainingView({ gcashQrExpiresAt: null, linkExpiresAt: null, effectiveExpiresAt: null }, deadline).state, 'unknown')
  assert.equal(formatGcashRemainingSeconds(3_661), '01:01:01')
  assert.equal(formatGcashRemainingSeconds(90_061), '1\u5929 01:01')
})
