// @ts-check

const config = {
  title: "Manor AI",
  tagline: "Self-hosted AI workspace runtime",
  favicon: "img/favicon.svg",
  url: "https://manor-os.github.io",
  baseUrl: "/docs/manor-ai/",
  organizationName: "manor-os",
  projectName: "manor-ai",
  trailingSlash: false,
  onBrokenLinks: "throw",
  onBrokenMarkdownLinks: "warn",
  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },
  presets: [
    [
      "classic",
      {
        docs: {
          routeBasePath: "/",
          sidebarPath: require.resolve("./sidebars.js"),
          editUrl: "https://github.com/manor-os/manor-ai/tree/main/docs-site/",
        },
        blog: false,
        theme: {
          customCss: require.resolve("./src/css/custom.css"),
        },
      },
    ],
  ],
  themes: [
    [
      require.resolve("@easyops-cn/docusaurus-search-local"),
      {
        hashed: true,
        language: ["en"],
        indexBlog: false,
        docsRouteBasePath: "/",
        highlightSearchTermsOnTargetPage: true,
      },
    ],
  ],
  themeConfig: {
    image: "img/social-card.png",
    colorMode: {
      defaultMode: "dark",
      disableSwitch: false,
      respectPrefersColorScheme: false,
    },
    navbar: {
      title: "Manor AI",
      logo: {
        alt: "Manor AI",
        src: "img/logo.svg",
        srcDark: "img/logo.svg",
      },
      items: [
        { type: "docSidebar", sidebarId: "mainSidebar", position: "left", label: "Docs" },
        { to: "/quickstart", label: "Quickstart", position: "left" },
        { to: "/api-reference", label: "API", position: "left" },
        { href: "https://github.com/manor-os/manor-ai", label: "GitHub", position: "right" },
      ],
    },
    footer: {
      style: "dark",
      links: [
        {
          title: "Project",
          items: [
            { label: "GitHub", href: "https://github.com/manor-os/manor-ai" },
            { label: "Security", to: "/security" },
            { label: "Contributing", to: "/development" },
          ],
        },
      ],
      copyright: `Copyright ${new Date().getFullYear()} Manor AI.`,
    },
    prism: {
      theme: require("prism-react-renderer").themes.github,
      darkTheme: require("prism-react-renderer").themes.dracula,
    },
  },
};

module.exports = config;
