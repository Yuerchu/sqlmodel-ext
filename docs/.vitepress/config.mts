import { withMermaid } from 'vitepress-plugin-mermaid'
import llmstxt from 'vitepress-plugin-llms'
import { copyOrDownloadAsMarkdownButtons } from 'vitepress-plugin-llms'
import { zh } from './zh.mts'
import { en } from './en.mts'

export default withMermaid({
  title: 'sqlmodel-ext',

  vite: {
    plugins: [
      llmstxt({
        // Exclude legacy redirect placeholders. The old guide/internals trees were
        // migrated to tutorials/how-to/reference/explanation under Diátaxis;
        // the placeholders only carry meta-refresh stubs and would pollute llms-full.txt.
        ignoreFiles: [
          'guide/*',
          'internals/*',
          'en/guide/*',
          'en/internals/*',
        ],
      }),
    ],
    optimizeDeps: {
      include: ['mermaid', 'dayjs'],
    },
    ssr: {
      noExternal: ['vitepress-plugin-mermaid', 'mermaid'],
    },
  },

  markdown: {
    config(md) {
      md.use(copyOrDownloadAsMarkdownButtons)
    },
  },

  locales: {
    root: {
      label: '简体中文',
      ...zh,
    },
    en: {
      label: 'English',
      ...en,
    },
  },

  themeConfig: {
    socialLinks: [
      { icon: 'github', link: 'https://github.com/Foxerine/sqlmodel-ext' },
    ],
    search: {
      provider: 'local',
    },
  },
})
