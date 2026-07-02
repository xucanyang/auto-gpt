import { theme } from 'antd'

const denseTokens = {
  fontSize: 13,
  fontSizeSM: 12,
  fontSizeLG: 14,
  controlHeight: 28,
  controlHeightSM: 24,
  controlHeightLG: 32,
  padding: 12,
  paddingSM: 8,
  paddingXS: 6,
  margin: 12,
  marginSM: 8,
  marginXS: 6,
  borderRadius: 6,
  borderRadiusLG: 8,
}

const denseComponents = {
  Button: {
    paddingInline: 10,
    paddingInlineSM: 7,
    onlyIconSize: 14,
  },
  Card: {
    headerHeight: 40,
    paddingLG: 14,
    padding: 12,
  },
  Descriptions: {
    itemPaddingBottom: 8,
  },
  Drawer: {
    paddingLG: 16,
  },
  Form: {
    itemMarginBottom: 12,
  },
  Input: {
    paddingBlock: 3,
    paddingInline: 8,
  },
  Menu: {
    itemHeight: 34,
    itemMarginBlock: 2,
    itemMarginInline: 6,
    iconSize: 14,
    fontSize: 13,
  },
  Pagination: {
    itemSize: 28,
    itemSizeSM: 22,
  },
  Select: {
    optionHeight: 28,
    optionPadding: '4px 8px',
  },
  Table: {
    cellFontSize: 12,
    cellPaddingBlock: 7,
    cellPaddingInline: 8,
    cellPaddingBlockMD: 7,
    cellPaddingInlineMD: 8,
    cellPaddingBlockSM: 5,
    cellPaddingInlineSM: 6,
    headerBorderRadius: 6,
  },
  Tabs: {
    horizontalMargin: '0 0 10px 0',
  },
  Tag: {
    defaultBg: 'rgba(122, 139, 163, 0.12)',
  },
}

const darkTheme = {
  token: {
    ...denseTokens,
    colorPrimary: '#6366f1',
    colorBgBase: '#1c1f2e',
    colorTextBase: '#f1f5f9',
    colorBgContainer: '#1c1f2e',
    colorBgElevated: '#252836',
    colorBorder: 'rgba(255,255,255,0.15)',
    borderRadius: 8,
    colorText: '#f1f5f9',
    colorTextSecondary: '#b0bcd4',
    colorTextTertiary: '#7a8ba3',
    colorBgLayout: '#13151e',
    colorBgSpotlight: 'rgba(99,102,241,0.2)',
  },
  components: {
    ...denseComponents,
    Layout: {
      siderBg: '#1c1f2e',
      triggerBg: '#1c1f2e',
      triggerColor: '#f1f5f9',
    },
  },
  algorithm: [theme.darkAlgorithm, theme.compactAlgorithm],
}

const lightTheme = {
  token: {
    ...denseTokens,
    colorPrimary: '#4f46e5',
    colorBgBase: '#ffffff',
    colorTextBase: '#0f172a',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: 'rgba(0,0,0,0.1)',
    borderRadius: 8,
    colorText: '#0f172a',
    colorTextSecondary: '#475569',
    colorTextTertiary: '#94a3b8',
    colorBgLayout: '#f8fafc',
  },
  components: {
    ...denseComponents,
    Layout: {
      siderBg: '#ffffff',
      triggerBg: '#ffffff',
      triggerColor: '#0f172a',
    },
  },
  algorithm: [theme.defaultAlgorithm, theme.compactAlgorithm],
}

export { darkTheme, lightTheme }
