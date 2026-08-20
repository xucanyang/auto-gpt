import { Alert, Form, Segmented } from 'antd'
import type { FormInstance } from 'antd'

import { normalizeDomainList } from '@/lib/domainList'
import {
  REGISTRATION_DOMAIN_TASK_MODE_COMBINED,
  REGISTRATION_DOMAIN_TASK_MODE_FIELD,
  REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
  normalizeRegistrationDomainTaskMode,
  registrationDomainTaskTotalTarget,
} from '@/lib/registrationDomainTasks'

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
    </>
  )
}
