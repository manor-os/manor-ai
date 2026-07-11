interface LoadingSpinnerProps {
  size?: number;
  className?: string;
}

export default function LoadingSpinner({ size = 24, className = "" }: LoadingSpinnerProps) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        className="stroke-stone-200"
        strokeWidth="3"
        fill="none"
      />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        className="stroke-manor-700"
        strokeWidth="3"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}
