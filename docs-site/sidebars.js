// @ts-check

const sidebars = {
  mainSidebar: [
    {
      type: "category",
      label: "Learn",
      collapsed: false,
      items: [
        "index",
        "quickstart",
        "installation",
        "concepts/agents",
        "concepts/skills-tools",
        "concepts/hitl-governance",
        "concepts/workspaces-knowledge",
      ],
    },
    {
      type: "category",
      label: "Operating",
      collapsed: false,
      items: [
        "configuration",
        "docker-compose",
        "architecture",
        "operations/sandbox",
        "operations/storage",
        "operations/backup-restore",
        "operations/upgrade-release",
        "troubleshooting",
        "security",
      ],
    },
    {
      type: "category",
      label: "Reference",
      collapsed: false,
      items: [
        "api-reference",
        "integrations/overview",
        "integrations/webhooks",
        "integrations/nango",
        "development",
      ],
    },
    {
      type: "link",
      label: "Roadmap",
      href: "https://github.com/manor-os/manor-ai/blob/main/ROADMAP.md",
    },
  ],
};

module.exports = sidebars;
