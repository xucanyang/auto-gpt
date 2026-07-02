# Browser extension wrapper for external AccessToken distribution

这个目录放的是给 MV3 浏览器插件用的最小封装。  
你可以直接把 `access-token-api-client.js` 拷进你的 extension 工程里。

## 基本用法

```js
import { ExternalAccessTokenApiClient } from './access-token-api-client.js'

const client = new ExternalAccessTokenApiClient({
  baseUrl: 'http://127.0.0.1:8000',
  apiToken: 'YOUR_ACCESS_TOKEN_API_TOKEN',
})

const claim = await client.claimAccessToken({
  consumer: 'my-extension',
  limit: 1,
  leaseSeconds: 86400,
  allowRefresh: true,
})

const item = claim.items?.[0]
if (!item) {
  throw new Error('no access token returned')
}

console.log(item.email, item.access_token)

await client.reportPaid(item.claim_id, {
  externalPaymentId: 'ext-001',
  message: 'stored successfully',
})
```

## 接口含义

- `claimAccessToken()`：领取一个可发的 AccessToken
- `getClaim(claimId)`：查询 claim 状态
- `reportPaid(claimId, ...)`：告诉本地服务“我已经成功接收/处理了这个 AT”
- `reportFailed(claimId, ...)`：告诉本地服务“这次没处理成功”
- `releaseClaim(claimId, ...)`：放弃本次领取

## 说明

- `paid` 在这里不是支付钱款，意思是**外部已成功接收并保存这个 token**。
- `failed` 表示**外部接收失败**，本地不会改账号状态。
- `claim` 返回后尽量只在内存里使用 `access_token`，不要长期明文落盘。
- 如果你的插件跑在 Chrome MV3 service worker 里，记得在 manifest 里加对 `http://127.0.0.1:8000/*` 的 `host_permissions`。

