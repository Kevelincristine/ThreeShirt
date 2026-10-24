import { NavLink } from "react-router-dom";

export interface NavbarLink {
  href: string;
  label: string;
}

export interface NavbarAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}

interface NavbarProps {
  links: readonly NavbarLink[];
  action?: NavbarAction;
}

export function Navbar({ links, action }: NavbarProps) {
  return (
    <nav aria-label="Navegação principal" className="site-nav">
      <ul className="site-nav__list">
        {links.map((link) => (
          <li key={link.href}>
            <NavLink
              className={({ isActive }) =>
                isActive ? "site-nav__link is-active" : "site-nav__link"
              }
              end={link.href === "/"}
              to={link.href}
            >
              {link.label}
            </NavLink>
          </li>
        ))}
        {action && (
          <li>
            <button
              className="site-nav__link"
              disabled={action.disabled}
              onClick={action.onClick}
              type="button"
            >
              {action.label}
            </button>
          </li>
        )}
      </ul>
    </nav>
  );
}
