<?php

/**
 * Clinical Co-Pilot custom module bootstrap entry point.
 *
 * This file is run by the OpenEMR module loader. It registers the module's
 * PSR-4 namespace and wires the Bootstrap class into the event dispatcher.
 *
 * @package OpenEMR\Modules\Copilot
 * @link    https://www.open-emr.org
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

/**
 * @var \OpenEMR\Core\ModulesClassLoader $classLoader Injected by the OpenEMR module loader.
 */
$classLoader->registerNamespaceIfNotExists('OpenEMR\\Modules\\Copilot\\', __DIR__ . DIRECTORY_SEPARATOR . 'src');

/**
 * @var \Symfony\Component\EventDispatcher\EventDispatcherInterface $eventDispatcher Injected by the OpenEMR module loader.
 */
$bootstrap = new Bootstrap($eventDispatcher);
$bootstrap->subscribeToEvents();
