import os

filepath = "frontend/src/pages/Accounts.tsx"
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()
    
# Import
content = content.replace(
    "import { Sub2ApiOverviewPanel } from '../features/accounts/components/Sub2ApiOverviewPanel'",
    "import { Sub2ApiOverviewPanel } from '../features/accounts/components/Sub2ApiOverviewPanel'\nimport { OaipayOverviewPanel } from '../features/accounts/components/OaipayOverviewPanel'"
)

# Panel inclusion
content = content.replace(
    "<Sub2ApiOverviewPanel overview={sub2apiOverview} />",
    "<Sub2ApiOverviewPanel overview={sub2apiOverview} />\n                  <OaipayOverviewPanel overview={oaipayOverview} />"
)

# sub2apiState to oaipayState logic
content = content.replace(
    "const [sub2apiState, setSub2apiState] = useState<string>('')",
    "const [sub2apiState, setSub2apiState] = useState<string>('')\n  const [oaipayState, setOaipayState] = useState<string>('')"
)

content = content.replace(
    "sub2apiState,",
    "sub2apiState,\n      oaipayState,"
)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)
