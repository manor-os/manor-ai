interface IconBoxProps {
  size?: "sm" | "md";
  children: React.ReactNode;
  className?: string;
}

export default function IconBox({ size = "md", children, className = "" }: IconBoxProps) {
  return (
    <div className={`${size === "sm" ? "manor-icon-box-sm" : "manor-icon-box"} ${className}`}>
      {children}
    </div>
  );
}
