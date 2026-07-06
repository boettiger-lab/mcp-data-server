export default {
  title: 'MCP Data Server',
  description: 'An open MCP server that connects AI agents to cloud-native data — grounding them in STAC metadata and validated query engines (DuckDB on S3).',
  base: '/mcp-data-server/',

  // Internal planning/spec artifacts, not part of the published site. Their
  // Go-template `{{ ... }}` syntax also breaks VitePress's Vue parser.
  srcExclude: ['superpowers/**'],

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/guide/quickstart' },
      { text: 'Datasets', link: '/guide/datasets' },
      { text: 'Vision', link: '/guide/vision' },
      { text: 'Roadmap', link: '/guide/roadmap' },
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
        text: 'About',
        items: [
          { text: 'The bigger picture', link: '/guide/vision' },
          { text: 'Roadmap', link: '/guide/roadmap' },
        ],
      },
      {
        text: 'Operations',
        items: [
          { text: 'Deployment', link: '/guide/deployment' },
          { text: 'Architecture', link: '/guide/architecture' },
          { text: 'Mirror failover (S3 outage)', link: '/guide/mirror-failover' },
        ],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/boettiger-lab/mcp-data-server' },
    ],

    footer: {
      message: 'Released under the BSD-3-Clause License.',
    },

    search: {
      provider: 'local',
    },
  },
}
