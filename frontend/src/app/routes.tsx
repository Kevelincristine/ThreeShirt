import { createBrowserRouter } from "react-router-dom";
import { App } from "./App";
import { HomePage } from "../pages/HomePage";
import { StudentLoginPage } from "../pages/StudentLoginPage";
import { StudentDashboardPage } from "../pages/StudentDashboardPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { RequireStudent } from "./RequireStudent";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "login", element: <StudentLoginPage /> },
      {
        path: "student",
        element: (
          <RequireStudent>
            <StudentDashboardPage />
          </RequireStudent>
        ),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
