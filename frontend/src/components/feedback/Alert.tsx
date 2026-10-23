import type { ReactNode } from "react";

export type AlertVariant = "info" | "success" | "warning" | "error";

interface AlertProps {
  children: ReactNode;
  variant?: AlertVariant;
}

export function Alert({ children, variant = "info" }: AlertProps) {
  return (
    <div
      className={`alert alert--${variant}`}
      role={variant === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
