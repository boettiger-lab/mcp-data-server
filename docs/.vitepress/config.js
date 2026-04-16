export default {
  title: 'MCP Data Server',
  description: 'MCP server providing SQL access to large-scale geospatial datasets via DuckDB and S3.',
  base: '/mcp-data-server/',

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/guide/quickstart' },
      { text: 'Datasets', link: '/guide/datasets' },
      { text: 'GitHub', link: 'https://github.com/boettiger-lab/mcp-data-server', target: '_blank' },
    ],

    sidebar: [
      {
        text: 'Getting Started',
        items: [
          { text: 'Quick Start', link: '/guide/quickstart' },
          { text: 'Available Datasets', link: '/guide/datasets' },
          { text: 'Private Data Access', link: '/guide/private-data' },
          { text: 'Programmatic Access (R & Python)', link: '/guide/programmatic-access' },
        ],
      },
      {
        text: 'Operations',
        items: [
          { text: 'Deployment', link: '/guide/deployment' },
          { text: 'Architecture', link: '/guide/architecture' },
        ],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/boettiger-lab/mcp-data-server' },
    ],

    footer: {
      message: 'Released under the MIT License.',
    },

    search: {
      provider: 'local',
    },
  },
}
