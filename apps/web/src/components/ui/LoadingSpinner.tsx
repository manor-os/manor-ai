interface LoadingSpinnerProps {
  size?: number;
  className?: string;
  ariaLabel?: string;
}

/** The one spinner. It takes the color of the text around it, so it works on any surface. */
export default function LoadingSpinner({ size = 20, className = "", ariaLabel }: LoadingSpinnerProps) {
  return (
    <svg
      role={ariaLabel ? "status" : undefined}
      aria-label={ariaLabel}
      className={`manor-spinner animate-spin ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.18" strokeWidth="2.5" fill="none" />
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" fill="none" />
    </svg>
  );
}
