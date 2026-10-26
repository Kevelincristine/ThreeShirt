import { createBrowserRouter } from "react-router-dom";
import { App } from "./App";
import { HomePage } from "../pages/HomePage";
import { StudentLoginPage } from "../pages/StudentLoginPage";
import { StudentDashboardPage } from "../pages/StudentDashboardPage";
import { StudentPreferencesPage } from "../pages/StudentPreferencesPage";
import { StudentProposalsPage } from "../pages/StudentProposalsPage";
import { StudentGalleryPage } from "../pages/StudentGalleryPage";
import { StudentResultsPage } from "../pages/StudentResultsPage";
import { AdminDashboardPage } from "../pages/AdminDashboardPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { RequireAdmin } from "./RequireAdmin";
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
          <RequireStudent allowAdmin>
            <StudentDashboardPage />
          </RequireStudent>
        ),
      },
      {
        path: "student/preferences",
        element: (
          <RequireStudent>
            <StudentPreferencesPage />
          </RequireStudent>
        ),
      },
      {
        path: "student/proposals",
        element: (
          <RequireStudent>
            <StudentProposalsPage />
          </RequireStudent>
        ),
      },
      {
        path: "student/gallery",
        element: (
          <RequireStudent allowAdmin>
            <StudentGalleryPage />
          </RequireStudent>
        ),
      },
      {
        path: "student/results",
        element: (
          <RequireStudent>
            <StudentResultsPage />
          </RequireStudent>
        ),
      },
      {
        path: "admin",
        element: (
          <RequireAdmin>
            <AdminDashboardPage />
          </RequireAdmin>
        ),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
