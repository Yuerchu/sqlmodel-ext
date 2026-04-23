import type { DefaultTheme } from 'vitepress'

export const en = {
  lang: 'en',
  description: 'SQLModel Enhancement Library — Tutorials, How-to, Reference, Explanation',

  themeConfig: {
    nav: [
      { text: 'Home', link: '/en/' },
      { text: 'Tutorials', link: '/en/tutorials/' },
      { text: 'How-to', link: '/en/how-to/' },
      { text: 'Reference', link: '/en/reference/' },
      { text: 'Explanation', link: '/en/explanation/' },
    ],

    sidebar: {
      '/en/tutorials/': [
        {
          text: 'Tutorials',
          items: [
            { text: 'Overview', link: '/en/tutorials/' },
            { text: '01 · Getting started', link: '/en/tutorials/01-getting-started' },
            { text: '02 · Building a blog API', link: '/en/tutorials/02-building-a-blog-api' },
            { text: '03 · Adding Redis caching', link: '/en/tutorials/03-adding-redis-cache' },
          ],
        },
      ],
      '/en/how-to/': [
        {
          text: 'How-to guides',
          items: [
            { text: 'Overview', link: '/en/how-to/' },
            { text: 'Integrate with FastAPI', link: '/en/how-to/integrate-with-fastapi' },
            { text: 'Paginate a list endpoint', link: '/en/how-to/paginate-a-list-endpoint' },
            { text: 'Define JTI models', link: '/en/how-to/define-jti-models' },
            { text: 'Define STI models', link: '/en/how-to/define-sti-models' },
            { text: 'Handle concurrent updates', link: '/en/how-to/handle-concurrent-updates' },
            { text: 'Prevent MissingGreenlet errors', link: '/en/how-to/prevent-missing-greenlet' },
            { text: 'Release the DB connection during long I/O', link: '/en/how-to/release-connection-during-long-io' },
            { text: 'Configure cascade delete', link: '/en/how-to/configure-cascade-delete' },
            { text: 'Cache queries with Redis', link: '/en/how-to/cache-queries' },
          ],
        },
      ],
      '/en/reference/': [
        {
          text: 'Reference',
          items: [
            { text: 'Overview', link: '/en/reference/' },
            { text: 'Base classes', link: '/en/reference/base-classes' },
            { text: 'CRUD methods', link: '/en/reference/crud-methods' },
            { text: 'Field types', link: '/en/reference/field-types' },
            { text: 'Mixins', link: '/en/reference/mixins' },
            { text: 'Decorators & helpers', link: '/en/reference/decorators' },
            { text: 'Pagination types', link: '/en/reference/pagination-types' },
          ],
        },
      ],
      '/en/explanation/': [
        {
          text: 'Explanation',
          items: [
            { text: 'Overview', link: '/en/explanation/' },
            { text: 'Prerequisites', link: '/en/explanation/prerequisites' },
            { text: 'Metaclass & SQLModelBase', link: '/en/explanation/metaclass' },
            { text: 'CRUD pipeline', link: '/en/explanation/crud-pipeline' },
            { text: 'Polymorphic internals', link: '/en/explanation/polymorphic-internals' },
            { text: 'Optimistic locking', link: '/en/explanation/optimistic-lock' },
            { text: 'Relation preloading', link: '/en/explanation/relation-preload' },
            { text: 'Cascade delete semantics', link: '/en/explanation/cascade-delete-semantics' },
            { text: 'Redis caching', link: '/en/explanation/cached-table' },
            { text: 'Static analyzer', link: '/en/explanation/relation-load-checker' },
          ],
        },
      ],
    },

    outline: {
      level: [2, 3],
      label: 'On this page',
    },

    docFooter: {
      prev: 'Previous',
      next: 'Next',
    },
  } satisfies DefaultTheme.Config,
}
