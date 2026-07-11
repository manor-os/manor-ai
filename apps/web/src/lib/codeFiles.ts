export type CodeFileReference = {
  name?: string | null;
  file_type?: string | null;
  mime_type?: string | null;
  fileType?: string | null;
  mimeType?: string | null;
};

const SPECIAL_FILENAME_LANGUAGE: Record<string, string> = {
  ".babelrc": "json",
  ".bash_profile": "bash",
  ".bashrc": "bash",
  ".dockerignore": "ignore",
  ".editorconfig": "editorconfig",
  ".env": "ini",
  ".eslintignore": "ignore",
  ".eslintrc": "json",
  ".gitattributes": "git",
  ".gitignore": "ignore",
  ".npmrc": "ini",
  ".prettierrc": "json",
  ".profile": "bash",
  ".vimrc": "vim",
  ".zprofile": "bash",
  ".zshrc": "bash",
  "brewfile": "ruby",
  "build": "python",
  "buck": "python",
  "caddyfile": "nginx",
  "cartfile": "ini",
  "cmakelists.txt": "cmake",
  "dockerfile": "docker",
  "earthfile": "docker",
  "gemfile": "ruby",
  "go.mod": "go-module",
  "go.sum": "go-module",
  "jenkinsfile": "groovy",
  "justfile": "makefile",
  "makefile": "makefile",
  "module.bazel": "python",
  "podfile": "ruby",
  "procfile": "bash",
  "rakefile": "ruby",
  "requirements.txt": "ini",
  "snakefile": "python",
  "taskfile": "yaml",
  "vagrantfile": "ruby",
  "workspace": "python",
};

const EXTENSION_LANGUAGE: Record<string, string> = {
  abap: "abap",
  abnf: "abnf",
  ada: "ada",
  agda: "agda",
  al: "al",
  antlr: "antlr4",
  apex: "apex",
  applescript: "applescript",
  apl: "apl",
  apib: "markdown",
  arff: "arff",
  arm: "armasm",
  asm: "nasm",
  asp: "aspnet",
  aspx: "aspnet",
  au3: "autoit",
  awk: "awk",
  bash: "bash",
  bat: "batch",
  bazel: "python",
  bicep: "bicep",
  bison: "bison",
  bnf: "bnf",
  bqn: "bqn",
  brainfuck: "brainfuck",
  brs: "brightscript",
  c: "c",
  cbl: "cobol",
  cc: "cpp",
  cfc: "cfscript",
  cfm: "cfscript",
  clj: "clojure",
  cljs: "clojure",
  cmake: "cmake",
  cob: "cobol",
  coffee: "coffeescript",
  conf: "nginx",
  cpp: "cpp",
  cql: "cypher",
  cr: "crystal",
  cs: "csharp",
  cshtml: "cshtml",
  css: "css",
  csv: "csv",
  cue: "cue",
  cxx: "cpp",
  cfg: "ini",
  cnf: "ini",
  d: "d",
  dart: "dart",
  diff: "diff",
  dockerfile: "docker",
  dockerignore: "ignore",
  dot: "dot",
  eex: "elixir",
  ejs: "ejs",
  elm: "elm",
  env: "ini",
  erb: "erb",
  erl: "erlang",
  ex: "elixir",
  exs: "elixir",
  f: "fortran",
  f03: "fortran",
  f90: "fortran",
  fish: "bash",
  fs: "fsharp",
  fsi: "fsharp",
  fsx: "fsharp",
  gd: "gdscript",
  gherkin: "gherkin",
  gitconfig: "git",
  gitignore: "ignore",
  glsl: "glsl",
  gm: "gml",
  gml: "gml",
  gn: "gn",
  go: "go",
  gradle: "gradle",
  graphql: "graphql",
  groovy: "groovy",
  gql: "graphql",
  h: "c",
  haml: "haml",
  handlebars: "handlebars",
  hbs: "handlebars",
  hcl: "hcl",
  hlsl: "hlsl",
  hpp: "cpp",
  hs: "haskell",
  htm: "markup",
  html: "markup",
  hx: "haxe",
  hxx: "cpp",
  idr: "idris",
  ipynb: "json",
  ini: "ini",
  java: "java",
  jinja: "django",
  jinja2: "django",
  jl: "julia",
  jq: "jq",
  js: "javascript",
  json: "json",
  json5: "json5",
  jsonc: "json",
  jsp: "java",
  jsx: "jsx",
  kt: "kotlin",
  kts: "kotlin",
  kusto: "kusto",
  latex: "latex",
  ld: "linker-script",
  less: "less",
  lisp: "lisp",
  liquid: "liquid",
  lock: "text",
  lsp: "lisp",
  lua: "lua",
  m: "objectivec",
  make: "makefile",
  mak: "makefile",
  md: "markdown",
  mdx: "markdown",
  mjs: "javascript",
  ml: "ocaml",
  mli: "ocaml",
  mm: "objectivec",
  mmd: "mermaid",
  module: "go-module",
  moon: "moonscript",
  nginx: "nginx",
  nim: "nim",
  nix: "nix",
  objc: "objectivec",
  pas: "pascal",
  patch: "diff",
  perl: "perl",
  php: "php",
  phtml: "php",
  pl: "perl",
  plist: "markup",
  plsql: "plsql",
  proto: "protobuf",
  properties: "properties",
  props: "markup",
  ps1: "powershell",
  pug: "pug",
  pyi: "python",
  py: "python",
  pyw: "python",
  qml: "qml",
  r: "r",
  rake: "ruby",
  rb: "ruby",
  re: "reason",
  rego: "rego",
  res: "rescript",
  rst: "rest",
  rs: "rust",
  sass: "sass",
  scala: "scala",
  scm: "scheme",
  scss: "scss",
  sh: "bash",
  shader: "glsl",
  sol: "solidity",
  sql: "sql",
  svg: "markup",
  svelte: "markup",
  swift: "swift",
  tcl: "tcl",
  tf: "hcl",
  tfvars: "hcl",
  toml: "toml",
  ts: "typescript",
  tsx: "tsx",
  twig: "twig",
  txt: "text",
  v: "verilog",
  vala: "vala",
  vb: "visual-basic",
  vbnet: "vbnet",
  vbs: "visual-basic",
  vert: "glsl",
  vhdl: "vhdl",
  vim: "vim",
  vue: "markup",
  wasm: "wasm",
  wgsl: "wgsl",
  xml: "markup",
  xquery: "xquery",
  xsd: "markup",
  xsl: "markup",
  xslt: "markup",
  yaml: "yaml",
  yml: "yaml",
  zig: "zig",
  zsh: "bash",
};

export const CODE_FILE_EXTENSIONS = new Set(Object.keys(EXTENSION_LANGUAGE).filter((ext) => ext !== "txt"));

const CODE_MIME_TYPES = new Set([
  "application/graphql",
  "application/javascript",
  "application/json",
  "application/ld+json",
  "application/sql",
  "application/typescript",
  "application/x-httpd-php",
  "application/x-javascript",
  "application/x-perl",
  "application/x-php",
  "application/x-python",
  "application/x-python-code",
  "application/x-ruby",
  "application/x-sh",
  "application/x-shellscript",
  "application/x-toml",
  "application/x-yaml",
  "application/xhtml+xml",
  "application/xml",
  "image/svg+xml",
  "text/css",
  "text/html",
  "text/javascript",
  "text/jsx",
  "text/markdown",
  "text/typescript",
  "text/tsx",
  "text/xml",
  "text/x-c",
  "text/x-c++",
  "text/x-java-source",
  "text/x-python",
  "text/x-ruby",
  "text/x-script.python",
  "text/x-shellscript",
  "text/x-sql",
]);

const MIME_LANGUAGE: Record<string, string> = {
  "application/graphql": "graphql",
  "application/javascript": "javascript",
  "application/json": "json",
  "application/ld+json": "json",
  "application/sql": "sql",
  "application/typescript": "typescript",
  "application/x-httpd-php": "php",
  "application/x-javascript": "javascript",
  "application/x-perl": "perl",
  "application/x-php": "php",
  "application/x-python": "python",
  "application/x-python-code": "python",
  "application/x-ruby": "ruby",
  "application/x-sh": "bash",
  "application/x-shellscript": "bash",
  "application/x-toml": "toml",
  "application/x-yaml": "yaml",
  "application/xhtml+xml": "markup",
  "application/xml": "markup",
  "image/svg+xml": "markup",
  "text/css": "css",
  "text/html": "markup",
  "text/javascript": "javascript",
  "text/jsx": "jsx",
  "text/markdown": "markdown",
  "text/typescript": "typescript",
  "text/tsx": "tsx",
  "text/xml": "markup",
  "text/x-c": "c",
  "text/x-c++": "cpp",
  "text/x-java-source": "java",
  "text/x-python": "python",
  "text/x-ruby": "ruby",
  "text/x-script.python": "python",
  "text/x-shellscript": "bash",
  "text/x-sql": "sql",
};

const LANGUAGE_LABELS: Record<string, string> = {
  bash: "Shell",
  batch: "Batch",
  clojure: "Clojure",
  cmake: "CMake",
  coffeescript: "CoffeeScript",
  cpp: "C++",
  csharp: "C#",
  css: "CSS",
  diff: "Diff",
  docker: "Dockerfile",
  editorconfig: "EditorConfig",
  fsharp: "F#",
  "go-module": "Go module",
  go: "Go",
  graphql: "GraphQL",
  hcl: "HCL",
  ini: "INI",
  java: "Java",
  javascript: "JavaScript",
  json: "JSON",
  json5: "JSON5",
  jsx: "JSX",
  kotlin: "Kotlin",
  latex: "LaTeX",
  makefile: "Makefile",
  markdown: "Markdown",
  markup: "HTML/XML",
  objectivec: "Objective-C",
  php: "PHP",
  powershell: "PowerShell",
  protobuf: "Protocol Buffers",
  python: "Python",
  ruby: "Ruby",
  rust: "Rust",
  scss: "SCSS",
  sql: "SQL",
  swift: "Swift",
  toml: "TOML",
  tsx: "TSX",
  typescript: "TypeScript",
  "visual-basic": "Visual Basic",
  yaml: "YAML",
};

function cleanMime(value?: string | null): string {
  return String(value || "").split(";")[0].trim().toLowerCase();
}

function basename(name?: string | null): string {
  return String(name || "").split(/[\\/]/).pop()?.toLowerCase() || "";
}

function extensionFromName(name?: string | null): string {
  const base = basename(name);
  if (!base) return "";
  if (base.startsWith(".env")) return "env";
  if (SPECIAL_FILENAME_LANGUAGE[base]) return base;
  const parts = base.split(".");
  if (parts.length === 1) return base;
  return parts.pop() || "";
}

function specialFilenameLanguage(name?: string | null): string | null {
  const base = basename(name);
  if (!base) return null;
  if (base.startsWith(".env")) return "ini";
  if (base.startsWith("dockerfile")) return "docker";
  return SPECIAL_FILENAME_LANGUAGE[base] || null;
}

function mimeFromReference(ref: CodeFileReference): string {
  return cleanMime(ref.mime_type || ref.mimeType || ref.file_type || ref.fileType);
}

export function isCodeLikeFile(ref: CodeFileReference | string): boolean {
  if (typeof ref === "string") {
    return Boolean(specialFilenameLanguage(ref)) || CODE_FILE_EXTENSIONS.has(extensionFromName(ref));
  }

  if (specialFilenameLanguage(ref.name)) return true;

  const ext = extensionFromName(ref.name);
  if (CODE_FILE_EXTENSIONS.has(ext)) return true;

  const fileType = cleanMime(ref.file_type || ref.fileType);
  if (fileType && (CODE_FILE_EXTENSIONS.has(fileType) || fileType in EXTENSION_LANGUAGE)) return true;

  const mime = mimeFromReference(ref);
  if (!mime) return false;
  return (
    CODE_MIME_TYPES.has(mime) ||
    mime.endsWith("+json") ||
    mime.endsWith("+xml") ||
    mime.startsWith("text/x-")
  );
}

export function codeLanguageForFile(ref: CodeFileReference | string): string {
  const name = typeof ref === "string" ? ref : ref.name;
  const special = specialFilenameLanguage(name);
  if (special) return special;

  const ext = extensionFromName(name);
  if (ext && EXTENSION_LANGUAGE[ext]) return EXTENSION_LANGUAGE[ext];

  if (typeof ref !== "string") {
    const fileType = cleanMime(ref.file_type || ref.fileType);
    if (fileType && EXTENSION_LANGUAGE[fileType]) return EXTENSION_LANGUAGE[fileType];

    const mime = mimeFromReference(ref);
    if (mime && MIME_LANGUAGE[mime]) return MIME_LANGUAGE[mime];
    if (mime.endsWith("+json")) return "json";
    if (mime.endsWith("+xml")) return "markup";
  }

  return "text";
}

export function codeLanguageLabel(ref: CodeFileReference | string): string {
  const language = codeLanguageForFile(ref);
  if (LANGUAGE_LABELS[language]) return LANGUAGE_LABELS[language];

  const name = typeof ref === "string" ? ref : ref.name;
  const ext = extensionFromName(name);
  if (ext && ext !== "text") return ext.toUpperCase();

  return "Code";
}
