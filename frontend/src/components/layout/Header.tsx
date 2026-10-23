import type { ReactNode } from "react";

interface HeaderProps {
  brand: ReactNode;
  children?: ReactNode;
}

export function Header({ brand, children }: HeaderProps) {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <div className="site-header__brand">{brand}</div>
        {children}
      </div>
    </header>
  );
}
