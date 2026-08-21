import { Alert, Col, Form, InputNumber, Row, Segmented } from 'antd'
import type { FormInstance } from 'antd'

import { normalizeDomainList } from '@/lib/domainList'
import {
  REGISTRATION_DOMAIN_TASK_MODE_COMBINED,
  REGISTRATION_DOMAIN_ACTIVE_SLOTS_FIELD,
  REGISTRATION_DOMAIN_TASK_MODE_FIELD,
  REGISTRATION_DOMAIN_NO_LINK_STREAK_FIELD,
  REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
  REGISTRATION_DOMAIN_TASK_MODE_ROTATING,
  REGISTRATION_DOMAIN_REJECTION_MIN_SAMPLES_FIELD,
  REGISTRATION_DOMAIN_REJECTION_THRESHOLD_FIELD,
  normalizeRegistrationDomainTaskMode,
  registrationDomainTaskTotalTarget,
} from '@/lib/registrationDomainTasks'
import { REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD } from '@/lib/registrationEligibilityCountry'
import { REGISTRATION_PAYPAL_LINK_ENABLED_FIELD } from '@/lib/registrationPaypalPayment'

type RegistrationDomainTaskModeFieldProps = {
  form: FormInstance
}

export function RegistrationDomainTaskModeField({ form }: RegistrationDomainTaskModeFieldProps) {
  const mode = normalizeRegistrationDomainTaskMode(
    Form.useWatch(REGISTRATION_DOMAIN_TASK_MODE_FIELD, form),
  )
  const domains = normalizeDomainList(Form.useWatch('tempmail_fixed_domains', form))
  const requestedCount = Math.max(1, Math.floor(Number(Form.useWatch('count', form)) || 1))
  const requestedConcurrency = Math.max(1, Math.floor(Number(Form.useWatch('concurrency', form)) || 1))
  const totalTarget = registrationDomainTaskTotalTarget(domains.length, requestedCount)
  const activeDomainSlots = Math.max(
    1,
    Math.floor(Number(Form.useWatch(REGISTRATION_DOMAIN_ACTIVE_SLOTS_FIELD, form)) || 1),
  )
  const rejectionThreshold = Number(Form.useWatch(REGISTRATION_DOMAIN_REJECTION_THRESHOLD_FIELD, form) ?? 50)
  const rejectionMinSamples = Math.max(
    1,
    Math.floor(Number(Form.useWatch(REGISTRATION_DOMAIN_REJECTION_MIN_SAMPLES_FIELD, form)) || 10),
  )
  const noLinkStreak = Math.max(
    1,
    Math.floor(Number(Form.useWatch(REGISTRATION_DOMAIN_NO_LINK_STREAK_FIELD, form)) || 10),
  )
  const zeroAmountEnabled = Boolean(Form.useWatch(REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD, form))
  const paymentLinkEnabled = Boolean(Form.useWatch(REGISTRATION_PAYPAL_LINK_ENABLED_FIELD, form))

  return (
    <>
      <Form.Item
        name={REGISTRATION_DOMAIN_TASK_MODE_FIELD}
        label="域名任务方式"
      >
        <Segmented
          block
          options={[
            { label: '合并任务', value: REGISTRATION_DOMAIN_TASK_MODE_COMBINED },
            { label: '按域名拆分', value: REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN },
            { label: '自动轮换', value: REGISTRATION_DOMAIN_TASK_MODE_ROTATING },
          ]}
        />
      </Form.Item>
      {mode === REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN && domains.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`将创建 ${domains.length} 个任务，每个任务目标 ${requestedCount} 个，总目标 ${totalTarget} 个`}
          description={`每个任务请求并发 ${requestedConcurrency}，所有任务共享当前实例的全局浏览器容量。`}
        />
      ) : null}
      {mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING ? (
        <>
          <Row gutter={12}>
            <Col xs={24} sm={12}>
              <Form.Item
                name={REGISTRATION_DOMAIN_ACTIVE_SLOTS_FIELD}
                label="同时运行域名数"
                initialValue={1}
                rules={[
                  { required: true, message: '请填写同时运行域名数' },
                  {
                    validator: async (_, value) => {
                      if (domains.length > 0 && Number(value) > domains.length) {
                        throw new Error(`不能超过本次勾选的 ${domains.length} 个域名`)
                      }
                    },
                  },
                ]}
              >
                <InputNumber min={1} max={Math.max(domains.length, 1)} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name={REGISTRATION_DOMAIN_REJECTION_THRESHOLD_FIELD}
                label="开户拒绝率高于"
                initialValue={50}
                rules={[{ required: true, message: '请填写开户拒绝率阈值' }]}
              >
                <InputNumber min={0} max={100} precision={1} suffix="%" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name={REGISTRATION_DOMAIN_REJECTION_MIN_SAMPLES_FIELD}
                label="开户决策最小样本"
                initialValue={10}
                rules={[{ required: true, message: '请填写最小样本数' }]}
              >
                <InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name={REGISTRATION_DOMAIN_NO_LINK_STREAK_FIELD}
                label="连续未提链阈值"
                initialValue={10}
                rules={[{ required: true, message: '请填写连续未提链阈值' }]}
              >
                <InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Alert
            type={zeroAmountEnabled && paymentLinkEnabled ? 'info' : 'error'}
            showIcon
            style={{ marginBottom: 16 }}
            message={
              zeroAmountEnabled && paymentLinkEnabled
                ? `保持 ${Math.min(activeDomainSlots, Math.max(domains.length, 1))} 个域名运行，按勾选顺序补位`
                : '自动轮换要求同时开启注册后 0 元检测和提链'
            }
            description={
              zeroAmountEnabled && paymentLinkEnabled
                ? `有效开户决策至少 ${rejectionMinSamples} 个且拒绝率严格高于 ${Number.isFinite(rejectionThreshold) ? rejectionThreshold : 50}%，或连续 ${noLinkStreak} 个业务终态未提链成功时，完成当前账号后切换域名。每任务请求并发 ${requestedConcurrency}，活动域名合计请求上限 ${Math.min(activeDomainSlots, Math.max(domains.length, 1)) * requestedConcurrency}，仍受实例全局浏览器容量限制。`
                : undefined
            }
          />
        </>
      ) : null}
    </>
  )
}
