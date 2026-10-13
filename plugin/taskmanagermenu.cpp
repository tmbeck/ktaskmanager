// Panel right-click menu: "Task Manager" on top of the stock org.kde.contextmenu actions.
#include <KConfigGroup>
#include <KPluginFactory>
#include <Plasma/Containment>
#include <Plasma/ContainmentActions>
#include <Plasma/PluginLoader>
#include <QAction>
#include <QIcon>
#include <QProcess>

class TaskManagerMenu : public Plasma::ContainmentActions
{
    Q_OBJECT
public:
    TaskManagerMenu(QObject *parent, const QVariantList &)
        : Plasma::ContainmentActions(parent)
    {
        m_launch = new QAction(QIcon::fromTheme(QStringLiteral("utilities-system-monitor")), QStringLiteral("Task Manager"), this);
        connect(m_launch, &QAction::triggered, [] {
            QProcess::startDetached(QStringLiteral("ktaskmanager"), {});
        });
        m_sep = new QAction(this);
        m_sep->setSeparator(true);
    }

    void restore(const KConfigGroup &config) override
    {
        m_config = config;
        if (m_inner) {
            m_inner->restore(config);
        }
    }

    void save(KConfigGroup &config) override
    {
        if (m_inner) {
            m_inner->save(config);
        }
    }

    QList<QAction *> contextualActions() override
    {
        if (!m_inner && containment()) {
            m_inner = Plasma::PluginLoader::self()->loadContainmentActions(containment(), QStringLiteral("org.kde.contextmenu"));
            if (m_inner) {
                m_inner->setParent(this);
                m_inner->setContainment(containment());
                m_inner->restore(m_config);
            }
        }
        QList<QAction *> actions{m_launch, m_sep};
        if (m_inner) {
            actions += m_inner->contextualActions();
        }
        return actions;
    }

private:
    QAction *m_launch;
    QAction *m_sep;
    Plasma::ContainmentActions *m_inner = nullptr;
    KConfigGroup m_config;
};

K_PLUGIN_CLASS_WITH_JSON(TaskManagerMenu, "taskmanagermenu.json")

#include "taskmanagermenu.moc"
