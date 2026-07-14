// @ts-check

const sidebars = {
  mainSidebar: [
    "index",
    "quickstart",
    "installation",
    "configuration",
    "docker-compose",
    "architecture",
    {
      type: "category",
      label: "Core Concepts",
      items: [
        "concepts/agents",
        "concepts/skills-tools",
        "concepts/hitl-governance",
        "concepts/workspaces-knowledge",
      ],
    },
    {
      type: "category",
      label: "Operations",
      items: [
        "operations/sandbox",
        "operations/storage",
        "operations/backup-restore",
        "operations/upgrade-release",
      ],
    },
    {
      type: "category",
      label: "Integrations",
      items: ["integrations/overview", "integrations/webhooks", "integrations/nango"],
    },
    "api-reference",
    "development",
    "troubleshooting",
    "security",
    {
      type: "link",
      label: "Roadmap",
      href: "https://github.com/manor-os/manor-ai/blob/main/ROADMAP.md",
    },
  ],
};

module.exports = sidebars;
